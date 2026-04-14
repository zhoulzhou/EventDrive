#!/usr/bin/env python3
import httpx
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)


class FinnhubIndexCrawler:
    source_name = "Finnhub 指数"

    def __init__(self):
        self.api_key = settings.FINNHUB_API_KEY
        self.base_url = "https://finnhub.io/api/v1"
        self.alert_messages = []

    async def fetch_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/quote"
        params = {
            "symbol": symbol,
            "token": self.api_key
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                logger.info(f"获取 {symbol} 报价成功: {data}")
                return data
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
        
        year_start = date(year, 1, 1)
        end_date = date(year, 1, 31)
        
        url = f"{self.base_url}/stock/candle"
        params = {
            "symbol": symbol,
            "resolution": "W",
            "from": int(datetime.combine(year_start, datetime.min.time()).timestamp()),
            "to": int(datetime.combine(end_date, datetime.max.time()).timestamp()),
            "token": self.api_key
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("s") == "ok" and data.get("c"):
                        first_close = data["c"][0]
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

    async def crawl(self):
        self.alert_messages = []
        logger.info("开始指数监控...")

        ndx_symbol = "NDX"
        vix_symbol = "VIX"

        ndx_quote = await self.fetch_quote(ndx_symbol)
        vix_quote = await self.fetch_quote(vix_symbol)

        if ndx_quote and ndx_quote.get("c", 0) > 0:
            current_price = ndx_quote["c"]
            year_start_price = await self.fetch_year_start_price(ndx_symbol)
            
            if year_start_price:
                drop_percent = ((year_start_price - current_price) / year_start_price) * 100
                alert_level = self.get_ndx_alert_level(drop_percent)
                
                self.alert_messages.append(f"📊 纳斯达克100指数 (NDX)")
                self.alert_messages.append(f"   当前价格: {current_price:.2f}")
                self.alert_messages.append(f"   年初价格: {year_start_price:.2f}")
                self.alert_messages.append(f"   年内涨跌幅: { -drop_percent:.2f}%")
                
                if alert_level:
                    self.alert_messages.append(f"   {alert_level}: 年内下跌 {drop_percent:.2f}%")
            else:
                self.alert_messages.append(f"📊 纳斯达克100指数 (NDX)")
                self.alert_messages.append(f"   当前价格: {current_price:.2f}")
                self.alert_messages.append(f"   警告: 无法获取年初价格")

        if vix_quote and vix_quote.get("c", 0) > 0:
            vix_value = vix_quote["c"]
            alert_level = self.get_vix_alert_level(vix_value)
            
            self.alert_messages.append("")
            self.alert_messages.append(f"📈 VIX 恐慌指数 (VIX)")
            self.alert_messages.append(f"   当前值: {vix_value:.2f}")
            
            if alert_level:
                self.alert_messages.append(f"   {alert_level}")

        if self.alert_messages:
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"【{settings.INDEX_KEYWORD}】指数监控报告 - {now}"
            full_message = header + "\n\n" + "\n".join(self.alert_messages)
            logger.info(f"指数监控结果:\n{full_message}")
            return full_message
        
        return None
