import asyncio
import httpx
import feedparser
from datetime import datetime
from typing import List, Dict, Any, Optional
from io import BytesIO
from app.crawlers.base import BaseCrawler, NewsItem


class APNewsCrawler(BaseCrawler):
    source_name: str = "美联社"

    def __init__(self):
        super().__init__()
        self.rss_url = "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(self.rss_url, headers=headers)
                response.raise_for_status()
                content = response.content

            feed = feedparser.parse(BytesIO(content))

            if not feed.entries:
                import logging
                logging.warning(f"[美联社] RSS解析后无条目，bozo={feed.bozo}, 内容长度={len(content)}")
                if len(content) > 0:
                    logging.warning(f"[美联社] 内容前200字符: {content[:200]}")
                if feed.bozo and hasattr(feed, 'bozo_exception'):
                    logging.warning(f"[美联社] bozo_exception: {feed.bozo_exception}")
                return []

            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", "")
                published = entry.get("published", "")

                if title and link:
                    raw_news_list.append({
                        "url": link,
                        "title": title,
                        "summary": self._clean_html(summary),
                        "publish_time": published
                    })
        except Exception as e:
            self.error_message = f"获取美联社RSS失败: {str(e)}"
            import logging
            logging.error(f"[美联社] 获取RSS异常: {e}")

        return raw_news_list

    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        return None

    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            url = raw_data.get("url", "")
            title = raw_data.get("title", "")

            if not url or not title:
                return None

            publish_time = self._parse_publish_time(raw_data.get("publish_time", ""))
            summary = raw_data.get("summary", "")

            return NewsItem(
                title=title,
                content=summary,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                author=None,
                summary=summary if summary else None,
                image_url=None,
                news_type="ap"
            )
        except Exception:
            return None

    def _parse_publish_time(self, time_str: str) -> datetime:
        if not time_str:
            return datetime.now()

        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(time_str)
            return dt.replace(tzinfo=None)
        except Exception:
            pass

        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%a, %d %b %Y %H:%M:%S"
        ]
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue

        return datetime.now()

    def _clean_html(self, html: str) -> str:
        import re
        if not html:
            return ""
        clean = re.sub(r'<[^>]+>', '', html)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    async def crawl(self) -> List[NewsItem]:
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            raw_news_list = await self.fetch_news_list()

            for raw_news in raw_news_list:
                news_item = self.parse_news_item(raw_news)

                if news_item:
                    self.news_list.append(news_item)

                    if len(self.news_list) >= 6:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list