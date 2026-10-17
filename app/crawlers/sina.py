"""新浪财经新闻爬虫（接口方式）。

数据来源：``https://feed.mix.sina.com.cn/api/roll/get``
返回标准 JSON，结构为 ``{"result":{"status":{"code":0},"data":[...]}}``。

参数说明：
- ``pageid=153`` 财经频道
- ``lid=2516`` 滚动新闻频道（7×24 快讯）
- ``num`` 每页条数，``page`` 页码

每条记录含 title / intro / url / ctime（秒级 Unix 时间戳）/ media_name / author 等字段。
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import async_random_delay, get_random_headers, get_limiter, RateLimitExceeded

logger = logging.getLogger(__name__)


class SinaNewsCrawler(BaseCrawler):
    source_name = "新浪财经"

    API = "https://feed.mix.sina.com.cn/api/roll/get"

    def __init__(self):
        super().__init__()
        self.referer = "https://finance.sina.com.cn/roll/"
        self.pageid = "153"
        self.lid = "2516"
        # 限流后单轮只取 1 页（30 条）即可满足 NEWS_PER_SOURCE=10
        self.max_pages = 1
        self.page_size = 30

    def _headers(self) -> Dict[str, str]:
        headers = get_random_headers()
        headers.update({
            "Referer": self.referer,
            "Accept": "application/json, text/plain, */*",
        })
        return headers

    def _fetch_page(self, page: int, num: int = None) -> List[Dict[str, Any]]:
        """带限流的分页请求：申请令牌 -> 请求 -> 成功/失败回报限流器。"""
        limiter = get_limiter()
        limiter.acquire(self.API)
        try:
            resp = httpx.get(
                self.API,
                params={
                    "pageid": self.pageid,
                    "lid": self.lid,
                    "k": "",
                    "num": num or self.page_size,
                    "page": page,
                },
                headers=self._headers(),
                timeout=20,
            )
            resp.raise_for_status()
        except RateLimitExceeded:
            raise
        except Exception as e:
            cool = limiter.report_failure(self.API)
            self.error_message = f"新浪请求失败: {e}（退避 {cool:.0f}s）"
            raise
        limiter.report_success(self.API)
        payload = resp.json()
        status = (payload.get("result") or {}).get("status") or {}
        if status.get("code") not in (0, "0"):
            raise RuntimeError(f"新浪接口返回异常: {status.get('msg')}")
        return (payload.get("result") or {}).get("data") or []

    @staticmethod
    def _parse_ts(ts: Any) -> datetime:
        """接口的 ctime 是秒级 Unix 时间戳（北京时间）。"""
        try:
            return datetime.fromtimestamp(int(ts))
        except (TypeError, ValueError, OSError):
            return datetime.now()

    def _normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title or not url:
            return None
        # 新浪的 intro 字段开头常带【导读】标记，作为摘要直接使用
        intro = (item.get("intro") or item.get("summary") or "").strip()
        return {
            "title": title,
            "url": url,
            "publish_time": self._parse_ts(item.get("ctime")),
            "summary": intro,
            "content": intro,
            "image_url": None,
            "media_name": (item.get("media_name") or item.get("author") or "").strip()
            or self.source_name,
        }

    def _fetch_sync(self) -> List[Dict[str, Any]]:
        """抓取新浪财经滚动新闻（单轮 1 页）。

        限流策略（2026-10-17）：begin_run 标记任务开始；单轮只取 1 页、
        抓取量收敛到「保留条数 + 少量冗余」，不做多页连翻。
        """
        from app.config import settings

        get_limiter().begin_run(self.API)
        want = min(int(settings.NEWS_PER_SOURCE * 1.5) + 5, 25)
        news_list: List[Dict[str, Any]] = []
        seen_urls = set()
        for page in range(1, self.max_pages + 1):
            if len(news_list) >= want:
                break
            try:
                items = self._fetch_page(page)
                logger.info(f"[新浪财经] 第 {page} 页返回 {len(items)} 条")
            except RateLimitExceeded as e:
                logger.warning(f"[新浪财经] 触发限流，停止本轮抓取: {e}")
                break
            except Exception as e:
                logger.error(f"[新浪财经] 第 {page} 页抓取失败: {e}")
                self.error_message = f"新浪接口失败: {e}"
                break

            for raw in items:
                norm = self._normalize(raw)
                if not norm or norm["url"] in seen_urls:
                    continue
                seen_urls.add(norm["url"])
                news_list.append(norm)

        news_list.sort(key=lambda x: x["publish_time"], reverse=True)
        logger.info(f"[新浪财经] 共获取 {len(news_list)} 条")
        return news_list

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_sync)

    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            title = raw_data.get("title", "")
            if not title:
                return None
            return NewsItem(
                title=title,
                content=raw_data.get("content") or raw_data.get("summary") or "",
                source=self.source_name,
                publish_time=raw_data.get("publish_time") or datetime.now(),
                url=raw_data.get("url", ""),
                author=raw_data.get("media_name") or None,
                summary=raw_data.get("summary") or None,
                image_url=raw_data.get("image_url") or None,
            )
        except Exception as e:
            self.error_message = f"解析失败: {e}"
            logger.error(f"[新浪财经] 解析失败: {e}")
            return None

    async def fetch_news_detail(self, url: str) -> Dict[str, Any]:
        """接口的 intro 已含内容要点，不再回源抓详情。"""
        return {}

    async def crawl(self) -> List[NewsItem]:
        from app.config import settings

        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            raw_news_list = await self.fetch_news_list()
            limit = settings.NEWS_PER_SOURCE

            for raw_news in raw_news_list[:limit]:
                news_item = self.parse_news_item(raw_news)
                if news_item and self.is_within_time_range(news_item.publish_time):
                    self.news_list.append(news_item)
                    await async_random_delay(min_delay=0.2, max_delay=0.6)

            logger.info(f"[新浪财经] 抓取完成，保留 {len(self.news_list)} 条")
        except Exception as e:
            self.error_message = str(e)
            logger.error(f"[新浪财经] crawl() 异常: {e}", exc_info=True)

        return self.news_list