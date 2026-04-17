#!/usr/bin/env python3
import httpx
import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional
from app.config import settings
from app.database import SessionLocal
from app import crud, schemas

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(timeout=15)


class FinnhubIndexCrawler:
    source_name = "腾讯指数"
    NDX_INITIAL_HIGH = 26011.75

    def __init__(self):
        self.alert_messages = []
        self.db = SessionLocal()

    async def fetch_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            tencent_map = {
                "NDX": "usNDX",
                "VIX": "usVIX"
            }
            code = tencent_map.get(symbol)
            if not code:
                return None

            url = f"http://qt.gtimg.cn/q={code}"
            await asyncio.sleep(0.5)
            response = await _client.get(url)
            if response.status_code != 200:
                logger.error(f"请求失败 {symbol}: {response.status_code}")
                return None

            content = response.text
            match = re.search(rf'{code}="([^"]+)"', content)
            if not match:
                logger.warning(f"⚠️ {symbol} 未找到数据")
                return None

            parts = match.group(1).split("~")
            try:
                current = float(parts[3])
                pre_close = float(parts[4])
                open_price = float(parts[5])
                high = float(parts[33]) if parts[33] and parts[33].replace(".", "").replace("-", "").isdigit() else current
                low = float(parts[34]) if parts[34] and parts[34].replace(".", "").replace("-", "").isdigit() else current
                update_time = parts[30] if len(parts) > 30 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if high == 0:
                    high = current
                if low == 0:
                    low = current

                quote = {
                    "c": current,
                    "h": high,
                    "l": low,
                    "o": open_price,
                    "pc": pre_close,
                    "update_time": update_time
                }
                logger.info(f"获取 {symbol} 报价成功: {quote}")
                return quote
            except (ValueError, IndexError):
                logger.warning(f"⚠️ {symbol} 解析数据失败")
                logger.warning(f"原始数据: {content}")
                return None
        except Exception as e:
            logger.error(f"获取 {symbol} 报价失败: {e}", exc_info=True)
            return None

    def get_vix_alert_level(self, vix_value: float) -> Optional[str]:
        if vix_value >= 30:
            return "🔴 极度恐慌警报"
        elif vix_value >= 25:
            return "🟠 恐慌预警"
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

    def get_ndx_high_alert(self, current_price: float, year_high: float) -> Optional[str]:
        if current_price > year_high:
            return None
        drop_from_high = ((year_high - current_price) / year_high) * 100
        if drop_from_high >= 10:
            return f"⚠️ 偏离年内高点 {drop_from_high:.2f}%"
        elif drop_from_high >= 5:
            return f"📉 偏离年内高点 {drop_from_high:.2f}%"
        elif drop_from_high >= 3:
            return f"📊 偏离年内高点 {drop_from_high:.2f}%"
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

        vix_alert_level = None
        ndx_high_updated = False
        ndx_update_time = ndx_quote.get("update_time") if ndx_quote else None
        vix_update_time = vix_quote.get("update_time") if vix_quote else None

        year_high = self.NDX_INITIAL_HIGH

        if ndx_quote and ndx_quote.get("c", 0) > 0:
            current_price = ndx_quote["c"]
            logger.info(f"📊 NDX 当前价格: {current_price}")

            high_result = self.update_index_high(ndx_symbol, current_price)
            ndx_high_updated = high_result.get('updated', False) if high_result else False

            if high_result:
                year_high = high_result['new_high']

            ndx_high_alert = self.get_ndx_high_alert(current_price, year_high)
            drop_percent = ((year_high - current_price) / year_high) * 100
            ndx_alert_level = self.get_ndx_alert_level(drop_percent)

            self.alert_messages.append(f"📊 纳斯达克100指数 (NDX)")
            self.alert_messages.append(f"   当前价格: {current_price:.2f}  ({ndx_update_time})")
            self.alert_messages.append(f"   年内高点: {year_high:.2f}")

            if ndx_high_updated:
                self.alert_messages.append(f"   🎉 突破历史新高! (前高: {high_result['old_high']})")
                has_alert = True

            if ndx_high_alert:
                self.alert_messages.append(f"   {ndx_high_alert}")
                has_alert = True

            if ndx_alert_level:
                self.alert_messages.append(f"   {ndx_alert_level}")
                has_alert = True

        if vix_quote and vix_quote.get("c", 0) > 0:
            vix_value = vix_quote["c"]
            vix_alert_level = self.get_vix_alert_level(vix_value)

            self.alert_messages.append("")
            self.alert_messages.append(f"📈 VIX 恐慌指数 (VIX)")
            self.alert_messages.append(f"   当前值: {vix_value:.2f}  ({vix_update_time})")

            if vix_alert_level:
                self.alert_messages.append(f"   {vix_alert_level}")
                has_alert = True

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
