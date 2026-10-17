"""东方财富新闻爬虫（接口方式）。

数据来源为东方财富的两个公开 JSON 接口，不再依赖 Playwright 渲染页面：

1. ``np-listapi.eastmoney.com/comm/web/getNewsByColumns``
   财经导读深度新闻。返回标准 JSON，字段含 title / summary / showTime /
   uniqueUrl / image / mediaName，一条记录即一篇文章。

2. ``newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html``
   7x24 全球财经快讯。返回 ``var ajaxResult={...};`` 包裹的 JSON，需剥掉前缀。
   快讯更新频率高，用来补足深度新闻的条数。

两者都只需普通 HTTP GET，无需浏览器、无需 cookie，因此比 Playwright 轻得多，
单轮抓取从「秒级起 + 5s 等待」降到百毫秒级。
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import async_random_delay, get_random_headers, get_limiter, RateLimitExceeded

logger = logging.getLogger(__name__)


class EastmoneyDepthCrawler(BaseCrawler):
    source_name = "东方财富"

    # 财经导读：column=345 对应 finance.eastmoney.com 的深度导读栏目
    LIST_API = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
    # 7x24 快讯：末位的 50 / 1 分别是条数与页码
    KUAIXUN_API = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"

    REQUEST_TIMEOUT = 20

    def __init__(self):
        super().__init__()
        self.list_fields = (
            "code,showTime,title,mediaName,summary,image,url,uniqueUrl,Np_dst"
        )

    def _headers(self, referer: str) -> Dict[str, str]:
        headers = get_random_headers()
        headers.update({
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        })
        return headers

    def _get(self, url: str, **kwargs) -> Any:
        """带限流的同步 GET：申请令牌 -> 发请求 -> 成功/失败回报限流器。"""
        limiter = get_limiter()
        limiter.acquire(url)
        try:
            resp = httpx.get(url, **kwargs)
            resp.raise_for_status()
        except RateLimitExceeded:
            raise
        except Exception as e:
            cool = limiter.report_failure(url)
            self.error_message = f"请求被限流/失败: {e}（退避 {cool:.0f}s）"
            raise
        limiter.report_success(url)
        return resp

    # ------------------------------------------------------------------
    # 财经导读
    # ------------------------------------------------------------------
    def _fetch_list_api(self, page_size: int) -> List[Dict[str, Any]]:
        params = {
            "client": "web",
            "biz": "web_news_col",
            "column": "345",
            "order": "1",
            "needInteractData": "0",
            "page_index": "1",
            "page_size": str(page_size),
            "req_trace": str(int(datetime.now().timestamp() * 1000)),
            "fields": self.list_fields,
            "types": "1,20",
            "or_tags": "",
        }
        resp = self._get(
            self.LIST_API,
            params=params,
            headers=self._headers("https://finance.eastmoney.com/"),
            timeout=self.REQUEST_TIMEOUT,
        )
        payload = resp.json()
        if not payload.get("success") and str(payload.get("code")) != "1":
            raise RuntimeError(f"财经导读接口返回异常: {payload.get('message')}")
        return (payload.get("data") or {}).get("list") or []

    # ------------------------------------------------------------------
    # 7x24 快讯
    # ------------------------------------------------------------------
    def _fetch_kuaixun_api(self) -> List[Dict[str, Any]]:
        resp = self._get(
            self.KUAIXUN_API,
            headers=self._headers("https://kuaixun.eastmoney.com/"),
            timeout=self.REQUEST_TIMEOUT,
        )
        text = resp.text.strip()

        # 响应形如 var ajaxResult={"rc":1,...}; 剥掉 JS 包装后按 JSON 解析
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", text, re.DOTALL)
        if not match:
            raise RuntimeError(f"快讯接口响应格式异常: {text[:200]}")
        payload = json.loads(match.group(1))
        return payload.get("LivesList") or []

    # ------------------------------------------------------------------
    # 归一化为统一的原始结构
    # ------------------------------------------------------------------
    def _normalize_list_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        summary = (item.get("summary") or "").strip()
        return {
            "title": title,
            "url": item.get("uniqueUrl") or item.get("url") or "",
            "publish_time": self._parse_publish_time(item.get("showTime")),
            "summary": summary,
            "content": summary,
            "image_url": item.get("image") or None,
            "media_name": (item.get("mediaName") or "").strip(),
        }

    def _normalize_kuaixun_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = (item.get("title") or "").strip()
        if not title:
            return None
        digest = (item.get("digest") or "").strip()
        return {
            "title": title,
            "url": item.get("url_w") or item.get("url_m") or item.get("url_unique") or "",
            "publish_time": self._parse_publish_time(
                item.get("showtime") or item.get("ordertime")
            ),
            "summary": digest,
            "content": digest,
            "image_url": item.get("image") or None,
            "media_name": "东方财富快讯",
        }

    def _fetch_sync(self) -> List[Dict[str, Any]]:
        """先取财经导读，条数不够再用 7x24 快讯补齐，按 URL 去重。

        限流策略（2026-10-17）：
        - 任务开始标记 begin_run，重置本轮计数并检查冷却；
        - 单次抓取量收敛到「保留条数 + 少量冗余」，不再无节制放大；
        - 第二个接口仅在第一个接口取不够时才请求，避免每次都打满。
        """
        from app.config import settings

        limiter = get_limiter()
        limiter.begin_run(self.LIST_API)

        # 单次抓取量上限：只需够选出 NEWS_PER_SOURCE 条，留 50% 冗余即可，
        # 不再像原来那样 ×3 拉到 30 条（过度抓取容易触发来源方风控）
        want = min(int(settings.NEWS_PER_SOURCE * 1.5) + 5, 25)
        news_list: List[Dict[str, Any]] = []
        seen_urls = set()

        def _add(raw: Optional[Dict[str, Any]]) -> None:
            if not raw:
                return
            url = raw["url"]
            if not url or url in seen_urls:
                return
            seen_urls.add(url)
            news_list.append(raw)

        try:
            logger.info(f"[东方财富] 请求财经导读接口，目标 {want} 条")
            for item in self._fetch_list_api(want):
                _add(self._normalize_list_item(item))
            logger.info(f"[东方财富] 财经导读获取 {len(news_list)} 条")
        except Exception as e:
            logger.error(f"[东方财富] 财经导读接口失败: {e}")
            self.error_message = f"财经导读接口失败: {e}"

        if len(news_list) < want:
            try:
                before = len(news_list)
                for item in self._fetch_kuaixun_api():
                    _add(self._normalize_kuaixun_item(item))
                logger.info(f"[东方财富] 7x24 快讯补充 {len(news_list) - before} 条")
            except RateLimitExceeded as e:
                logger.warning(f"[东方财富] 快讯接口触发限流，跳过补齐: {e}")
            except Exception as e:
                logger.error(f"[东方财富] 7x24 快讯接口失败: {e}")
                self.error_message = f"{self.error_message or ''} 快讯接口失败: {e}".strip()

        news_list.sort(key=lambda x: x["publish_time"], reverse=True)
        logger.info(f"[东方财富] 接口抓取完成，共 {len(news_list)} 条")
        return news_list

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_sync)

    def _parse_publish_time(self, time_str: str) -> datetime:
        """接口返回 '2026-10-17 05:15:12' 这样的北京时间字符串。"""
        if not time_str:
            return datetime.now()
        text = time_str.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        logger.debug(f"[东方财富] 无法解析时间: {time_str}")
        return datetime.now()

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
            logger.error(f"[东方财富] 解析失败: {e}")
            return None

    async def fetch_news_detail(self, url: str) -> Dict[str, Any]:
        """接口返回的 summary 已含正文要点，不再单独回源抓详情。"""
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
                    # 接口调用很快，无需长延时，仅轻微错开避免突发限流
                    await async_random_delay(min_delay=0.2, max_delay=0.6)

            logger.info(f"[东方财富] 抓取完成，保留 {len(self.news_list)} 条")
        except Exception as e:
            self.error_message = str(e)
            logger.error(f"[东方财富] crawl() 异常: {e}", exc_info=True)

        return self.news_list