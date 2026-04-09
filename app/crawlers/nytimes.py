import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay


class NYTCrawler(BaseCrawler):
    source_name: str = "纽约时报"

    def __init__(self):
        super().__init__()
        self.api_key = "uXnQUhc3Y1pGqIDcd7u9F2A9h41SrfolGf2wBGCsN3A0KYyx"
        self.wire_api_url = "https://api.nytimes.com/svc/news/v3/content/all/all.json"
        self.topstories_api_url = "https://api.nytimes.com/svc/topstories/v2/home.json"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                wire_url = f"{self.wire_api_url}?api-key={self.api_key}"
                response = await client.get(wire_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "OK" and data.get("results"):
                    for item in data["results"][:6]:
                        title = item.get("title", "")
                        url = item.get("url", "")
                        abstract = item.get("abstract", "")
                        published_date = item.get("published_date", "")

                        if title and url:
                            raw_news_list.append({
                                "url": url,
                                "title": title,
                                "summary": abstract,
                                "source": "纽约时报",
                                "publish_time": published_date,
                                "section": item.get("section", ""),
                                "subsection": item.get("subsection", "")
                            })

            except Exception as e:
                self.error_message = f"获取纽约时报快讯失败: {str(e)}"

        return raw_news_list

    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return {
                        'url': url,
                        'html': response.text
                    }
            except Exception as e:
                self.error_message = f"获取纽约时报新闻详情失败 {url}: {str(e)}"
        return None

    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            url = raw_data.get('url', '')
            title = raw_data.get('title', '')

            if not url or not title:
                return None

            publish_time_str = raw_data.get('publish_time', '')
            display_time = self._parse_display_time(publish_time_str)

            summary = raw_data.get('summary', '')

            return NewsItem(
                title=title,
                content=summary,
                source=self.source_name,
                publish_time=display_time,
                url=url,
                author=None,
                summary=summary if summary else None,
                image_url=None,
                news_type="wire"
            )
        except Exception as e:
            self.error_message = f"解析纽约时报新闻失败: {str(e)}"
            return None

    def _parse_publish_time(self, time_str: str) -> datetime:
        if not time_str:
            return datetime.now()

        time_str = time_str.replace('Z', '+00:00')

        import re
        tz_match = re.search(r'([+-])(\d{2}):(\d{2})$', time_str)
        tz_offset_minutes = 0
        if tz_match:
            sign = tz_match.group(1)
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3))
            tz_offset_minutes = hours * 60 + minutes
            if sign == '-':
                tz_offset_minutes = -tz_offset_minutes
            time_str = re.sub(r'[+-]\d{2}:\d{2}$', '', time_str)

        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d"
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                if tz_offset_minutes:
                    from datetime import timezone, timedelta
                    tz = timezone(timedelta(minutes=tz_offset_minutes))
                    dt = dt.replace(tzinfo=tz)
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except ValueError:
                continue

        return datetime.now()

    def _parse_display_time(self, time_str: str) -> datetime:
        if not time_str:
            return datetime.now()

        time_str = time_str.replace('Z', '+00:00')

        import re
        tz_match = re.search(r'([+-])(\d{2}):(\d{2})$', time_str)
        if tz_match:
            sign = tz_match.group(1)
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3))
            tz_offset = hours * 60 + minutes
            if sign == '-':
                tz_offset = -tz_offset
            time_str = re.sub(r'[+-]\d{2}:\d{2}$', '', time_str)
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone(timedelta(minutes=tz_offset))
            )

        formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue

        return datetime.now()

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
                    random_delay(min_delay=1, max_delay=3)

                    if len(self.news_list) >= 6:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list


class NYTDepthCrawler(BaseCrawler):
    source_name: str = "纽约时报"

    def __init__(self):
        super().__init__()
        self.api_key = "uXnQUhc3Y1pGqIDcd7u9F2A9h41SrfolGf2wBGCsN3A0KYyx"
        self.wire_api_url = "https://api.nytimes.com/svc/news/v3/content/all/all.json"
        self.topstories_api_url = "https://api.nytimes.com/svc/topstories/v2/home.json"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []

        wire_results = await self._fetch_wire_news()
        raw_news_list.extend(wire_results)

        top_results = await self._fetch_topstories()
        raw_news_list.extend(top_results)

        return raw_news_list

    async def _fetch_wire_news(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                wire_url = f"{self.wire_api_url}?api-key={self.api_key}"
                response = await client.get(wire_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "OK" and data.get("results"):
                    for item in data["results"][:6]:
                        title = item.get("title", "")
                        url = item.get("url", "")
                        abstract = item.get("abstract", "")
                        published_date = item.get("published_date", "")

                        if title and url:
                            raw_news_list.append({
                                "url": url,
                                "title": title,
                                "summary": abstract,
                                "source": "纽约时报",
                                "publish_time": published_date,
                                "section": item.get("section", ""),
                                "subsection": item.get("subsection", ""),
                                "news_type": "wire"
                            })

            except Exception as e:
                self.error_message = f"获取纽约时报快讯失败: {str(e)}"

        return raw_news_list

    async def _fetch_topstories(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                top_url = f"{self.topstories_api_url}?api-key={self.api_key}"
                response = await client.get(top_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                if data.get("status") == "OK" and data.get("results"):
                    for item in data["results"][:6]:
                        title = item.get("title", "")
                        url = item.get("url", "")
                        abstract = item.get("abstract", "")
                        published_date = item.get("published_date", "")

                        if title and url:
                            raw_news_list.append({
                                "url": url,
                                "title": title,
                                "summary": abstract,
                                "source": "纽约时报",
                                "publish_time": published_date,
                                "section": item.get("section", ""),
                                "subsection": item.get("subsection", ""),
                                "news_type": "topstories"
                            })

            except Exception as e:
                self.error_message = f"获取纽约时报头条失败: {str(e)}"

        return raw_news_list

    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return {
                        'url': url,
                        'html': response.text
                    }
            except Exception as e:
                self.error_message = f"获取纽约时报新闻详情失败 {url}: {str(e)}"
        return None

    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            url = raw_data.get('url', '')
            title = raw_data.get('title', '')

            if not url or not title:
                return None

            publish_time_str = raw_data.get('publish_time', '')
            display_time = self._parse_display_time(publish_time_str)

            summary = raw_data.get('summary', '')
            news_type = raw_data.get('news_type')

            return NewsItem(
                title=title,
                content=summary,
                source=self.source_name,
                publish_time=display_time,
                url=url,
                author=None,
                summary=summary if summary else None,
                image_url=None,
                news_type=news_type
            )
        except Exception as e:
            self.error_message = f"解析纽约时报新闻失败: {str(e)}"
            return None

    def _parse_publish_time(self, time_str: str) -> datetime:
        if not time_str:
            return datetime.now()

        time_str = time_str.replace('Z', '+00:00')

        import re
        tz_match = re.search(r'([+-])(\d{2}):(\d{2})$', time_str)
        tz_offset_minutes = 0
        if tz_match:
            sign = tz_match.group(1)
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3))
            tz_offset_minutes = hours * 60 + minutes
            if sign == '-':
                tz_offset_minutes = -tz_offset_minutes
            time_str = re.sub(r'[+-]\d{2}:\d{2}$', '', time_str)

        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d"
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                if tz_offset_minutes:
                    from datetime import timezone, timedelta
                    tz = timezone(timedelta(minutes=tz_offset_minutes))
                    dt = dt.replace(tzinfo=tz)
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except ValueError:
                continue

        return datetime.now()

    def _parse_display_time(self, time_str: str) -> datetime:
        if not time_str:
            return datetime.now()

        time_str = time_str.replace('Z', '+00:00')

        import re
        tz_match = re.search(r'([+-])(\d{2}):(\d{2})$', time_str)
        if tz_match:
            sign = tz_match.group(1)
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3))
            tz_offset = hours * 60 + minutes
            if sign == '-':
                tz_offset = -tz_offset
            time_str = re.sub(r'[+-]\d{2}:\d{2}$', '', time_str)
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone(timedelta(minutes=tz_offset))
            )

        formats = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue

        return datetime.now()

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
                    random_delay(min_delay=1, max_delay=3)

                    if len(self.news_list) >= 12:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list