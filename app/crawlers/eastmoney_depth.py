import logging
import time
from datetime import datetime
from typing import Any, Dict, List

import httpx

from app.config import settings
from app.crawlers.base import BaseCrawler, NewsItem

logger = logging.getLogger(__name__)


class EastmoneyDepthCrawler(BaseCrawler):
    """东方财富新闻爬虫（接口版）。

    原实现用 Playwright 打开 finance.eastmoney.com/a/ccjdd.html 解析 DOM，
    页面改版或反爬都会导致抓取失败。现改为直接请求东方财富新闻列表接口
    np-listapi/comm/web/getNewsByColumns（column=350 财经导读），不再启动浏览器。
    """

    source_name = "东方财富"
    API_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
    COLUMN = "350"  # 财经导读

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        params = {
            "client": "web",
            "biz": "web_news_col",
            "column": self.COLUMN,
            "order": "1",
            "needInteractData": "0",
            "page_index": "1",
            "page_size": str(max(settings.NEWS_PER_SOURCE, 10)),
            "req_trace": str(int(time.time() * 1000)),
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://finance.eastmoney.com/a/ccjdd.html",
        }

        logger.info(f"[东方财富] 请求新闻接口, column={self.COLUMN}, page_size={params['page_size']}")
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(self.API_URL, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as e:
            self.error_message = f"请求东方财富新闻接口失败: {e}"
            logger.error(f"[东方财富] {self.error_message}", exc_info=True)
            return []

        if str(payload.get("code")) != "1":
            self.error_message = f"东方财富新闻接口返回异常: {payload.get('message')}"
            logger.error(f"[东方财富] {self.error_message}")
            return []

        items = (payload.get("data") or {}).get("list") or []
        logger.info(f"[东方财富] 接口返回 {len(items)} 条新闻")
        return [self._to_raw_news(item) for item in items if item.get("title")]

    def _to_raw_news(self, item: Dict[str, Any]) -> Dict[str, Any]:
        summary = (item.get("summary") or "").strip()
        return {
            "title": (item.get("title") or "").strip(),
            "url": item.get("uniqueUrl") or item.get("url") or "",
            "publish_time": self._parse_publish_time(item.get("showTime") or ""),
            "summary": summary,
            "content": summary,
            "author": (item.get("mediaName") or "").strip() or None,
        }

    def _parse_publish_time(self, time_str: str) -> datetime:
        try:
            return datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.now()

    def parse_news_item(self, raw_data):
        try:
            title = raw_data.get("title", "")
            if not title:
                return None

            return NewsItem(
                title=title,
                content=raw_data.get("content", raw_data.get("summary", "")),
                source=self.source_name,
                publish_time=raw_data.get("publish_time", datetime.now()),
                url=raw_data.get("url", ""),
                author=raw_data.get("author"),
                summary=raw_data.get("summary"),
                image_url=None
            )
        except Exception as e:
            self.error_message = f"解析失败: {str(e)}"
            return None

    async def fetch_news_detail(self, url: str) -> dict:
        return {}
