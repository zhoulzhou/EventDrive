#!/usr/bin/env python3
import httpx
import asyncio
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional
from app.config import settings
from app.database import SessionLocal
from app import crud, schemas

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(timeout=10)


class FinnhubIndexCrawler:
    source_name = "腾讯指数"
    NDX_INITIAL_HIGH = 26011.75

    def __init__(self):
        self.alert_messages = []
        self.db = SessionLocal()

    async def fetch_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            if symbol == "NDX":
                url = "https://stooq.com/q/l/?s=ndx&f=sd2t2ohlcv&h&e=csv"
            elif symbol == "VIX":
                url = "https://stooq.com/q/l/?s=vix&f=sd2t2ohlcv&h&e=csv"
            else:
                return None

            await asyncio.sleep(0.5)
            response = await _client.get(url)
            if response.status_code != 200:
                logger.error(f"请求失败 {symbol}: {response.status_code}")
                return None

            lines = response.text.strip().splitlines()
            if len(lines) < 2:
                logger.error(f"未找到 {symbol} 数据")
                return None

            parts = lines[1].split(',')
            quote = {
                "c": float(parts[6]),
                "h": float(parts[4]),
                "l": float(parts[5]),
                "o": float(parts[3]),
                "pc": 0.0,
                "update_time": parts[1]
            }
            logger.info(f"获取 {symbol} 报价成功: {quote}")
            return quote
        except Exception as e:
            logger.error(f"获取 {symbol} 报价失败: {e}", exc_info=True)
            return None

    async def fetch_year_start_price(self, symbol: str) -> Optional[float]:
        from pathlib import Path
        import json

        data_file = settings.DATA_DIR / "index_prices.json"
        today = date.today()
        year = today.year

        if data_file.exists():
            try:
                with open(data_file, 'r') as f:
                    saved_data = json.load(f)
                    if symbol in saved_data and saved_data[symbol].get("year") == year:
                        price = saved_data[symbol]["price"]
                        logger.info(f"从本地文件加载 {symbol} 年初价格: {price}")
                        return price
            except Exception as e:
                logger.warning(f"读取本地价格文件失败: {e}")

        symbol_map = {
            "NDX": "%5ENDX",
            "VIX": "%5EVIX"
        }
        yahoo_symbol = symbol_map.get(symbol, symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"

        year_start_ts = int(datetime(year, 1, 1).timestamp())
        end_ts = int(datetime(year, 1, 2).timestamp())

        try:
            await asyncio.sleep(1)
            response = await _client.get(url, params={
                "period1": year_start_ts,
                "period2": end_ts,
                "interval": "1d"
            })
            if response.status_code != 200:
                logger.warning(f"{symbol} 请求失败: {response.status_code}")
                return None
            data = response.json()
            result = data["chart"]["result"]
            if result and result[0].get("indicators") and result[0]["indicators"].get("quote"):
                closes = result[0]["indicators"]["quote"][0].get("close")
                if closes:
                    first_close = next((c for c in closes if c is not None), None)
                    if first_close:
                        logger.info(f"{symbol} 年初价格: {first_close}")

                        try:
                            saved_data = {}
                            if data_file.exists():
                                with open(data_file, 'r') as f:
                                    saved_data = json.load(f)
                            saved_data[symbol] = {"year": year, "price": first_close}
                            with open(data_file, 'w') as f:
                                json.dump(saved_data, f)
                        except Exception as e:
                            logger.warning(f"保存本地价格文件失败: {e}")

                        return first_close
            logger.warning(f"{symbol} 未找到年初价格数据，使用默认估算")
            return None
        except Exception as e:
            logger.error(f"获取 {symbol} 年初价格失败: {e}", exc_info=True)
            return None

    def get_ndx_alert_level(self, drop_percent: float) -> Optional[str]:
        if drop_percent >= 30:
            return "🔴 崩盘严重警报"
        elif drop_percent >= 20:
            return "🔴 熊市警报"
        elif drop_percent >= 10:
            return "🟠 预警"
        elif drop_percent >= 5:
            return "🟡 关注"
        elif drop_percent >= 3:
            return "🟢 注意"
        return None

    def get_vix_alert_level(self, vix_value: float) -> Optional[str]:
        if vix_value >= 30:
            return "🔴 极度恐慌警报"
        elif vix_value >= 25:
            return "🟠 恐慌预警"
        return None

    def update_index_high(self, symbol: str, current_price: float) -> Optional[Dict[str, Any]]:
        try:
            index_high = crud.get_or_create_index_high(self.db, symbol, self.NDX_INITIAL_HIGH)
            if current_price > index_high.high_price:
                updated = crud.update_index_high(self.db, symbol, current_price)
                logger.info(f"{symbol} 历史高点更新: {index_high.high_price} -> {current_price}")
                return {"updated": True, "old_high": index_high.high_price, "new_high": current_price}
            else:
                logger.info(f"{symbol} 历史高点未更新: 当前 {current_price} <= 历史高点 {index_high.high_price}")
                return {"updated": False, "old_high": index_high.high_price, "new_high": index_high.high_price}
        except Exception as e:
            logger.error(f"更新 {symbol} 历史高点失败: {e}", exc_info=True)
            return None

    async def crawl(self):
        self.alert_messages = []
        has_alert = False
        logger.info("=" * 50)
        logger.info("📊 开始指数监控任务...")
        logger.info("=" * 50)

        ndx_symbol = "NDX"
        vix_symbol = "VIX"

        logger.info(f"🔍 正在获取 {ndx_symbol} 和 {vix_symbol} 指数数据...")

        ndx_quote = await self.fetch_quote(ndx_symbol)
        logger.info(f"📈 NDX 报价响应: {ndx_quote}")

        vix_quote = await self.fetch_quote(vix_symbol)
        logger.info(f"📈 VIX 报价响应: {vix_quote}")

        ndx_alert_level = None
        vix_alert_level = None
        ndx_high_updated = False
        ndx_update_time = ndx_quote.get("update_time") if ndx_quote else None
        vix_update_time = vix_quote.get("update_time") if vix_quote else None

        if ndx_quote and ndx_quote.get("c", 0) > 0:
            current_price = ndx_quote["c"]
            logger.info(f"📊 NDX 当前价格: {current_price}")

            year_start_price = await self.fetch_year_start_price(ndx_symbol)
            logger.info(f"📅 NDX 年初价格: {year_start_price}")

            high_result = self.update_index_high(ndx_symbol, current_price)
            ndx_high_updated = high_result.get('updated', False) if high_result else False

            if year_start_price:
                drop_percent = ((year_start_price - current_price) / year_start_price) * 100
                ndx_alert_level = self.get_ndx_alert_level(drop_percent)

                self.alert_messages.append(f"📊 纳斯达克100指数 (NDX)")
                self.alert_messages.append(f"   当前价格: {current_price:.2f}  ({ndx_update_time})")
                self.alert_messages.append(f"   年初价格: {year_start_price:.2f}")
                self.alert_messages.append(f"   年内涨跌幅: { -drop_percent:.2f}%")

                if high_result:
                    self.alert_messages.append(f"   历史高点: {high_result['new_high']}")
                    if ndx_high_updated:
                        self.alert_messages.append(f"   🎉 突破历史新高! (前高: {high_result['old_high']})")

                if ndx_alert_level:
                    self.alert_messages.append(f"   {ndx_alert_level}: 年内下跌 {drop_percent:.2f}%")
                    has_alert = True
                else:
                    self.alert_messages.append(f"   🟢 正常")
            else:
                self.alert_messages.append(f"📊 纳斯达克100指数 (NDX)")
                self.alert_messages.append(f"   当前价格: {current_price:.2f}  ({ndx_update_time})")

                if high_result:
                    self.alert_messages.append(f"   历史高点: {high_result['new_high']}")
                    if ndx_high_updated:
                        self.alert_messages.append(f"   🎉 突破历史新高! (前高: {high_result['old_high']})")

                self.alert_messages.append(f"   警告: 无法获取年初价格")

        if vix_quote and vix_quote.get("c", 0) > 0:
            vix_value = vix_quote["c"]
            vix_alert_level = self.get_vix_alert_level(vix_value)

            self.alert_messages.append("")
            self.alert_messages.append(f"📈 VIX 恐慌指数 (VIX)")
            self.alert_messages.append(f"   当前值: {vix_value:.2f}  ({vix_update_time})")

            if vix_alert_level:
                self.alert_messages.append(f"   {vix_alert_level}")
                has_alert = True
            else:
                self.alert_messages.append(f"   🟢 正常")

        logger.info(f"🔔 指数警报状态: has_alert={has_alert}, ndx_high_updated={ndx_high_updated}")

        should_push = has_alert or ndx_high_updated or (len(self.alert_messages) > 0)

        if self.alert_messages and should_push:
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"【{settings.INDEX_KEYWORD}】指数监控报告 - {now}"
            full_message = header + "\n\n" + "\n".join(self.alert_messages)
            logger.info(f"📋 指数监控结果:\n{full_message}")
            logger.info("=" * 50)
            logger.info("✅ 指数监控任务完成")
            logger.info("=" * 50)
            return full_message

        logger.info("ℹ️ 指数无明显变化，不推送飞书消息")
        logger.info("=" * 50)
        logger.info("✅ 指数监控任务完成")
        logger.info("=" * 50)
        return None

    def close(self):
        self.db.close()
