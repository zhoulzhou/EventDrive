import asyncio
from datetime import datetime
from pathlib import Path
import sys
import re
import concurrent.futures
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay


class EastmoneyDepthCrawler(BaseCrawler):
    source_name = "东方财富"

    def __init__(self):
        super().__init__()
        self.base_url = "https://finance.eastmoney.com"

    def _fetch_sync(self):
        news_list = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                locale="zh-CN"
            )
            page = context.new_page()

            try:
                page.goto(f"{self.base_url}/a/ccjdd.html", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(5000)

                items = page.query_selector_all('#newsListContent > li')

                seen_urls = set()
                for item in items:
                    try:
                        title_elem = item.query_selector('p.title a')
                        if not title_elem:
                            continue

                        href = title_elem.get_attribute('href')
                        if not href or href in seen_urls:
                            continue
                        seen_urls.add(href)

                        title = title_elem.inner_text()
                        title = title.strip()

                        if not title or title == "None" or len(title) < 5:
                            continue

                        time_elem = item.query_selector('p.time')
                        publish_time_str = time_elem.inner_text() if time_elem else ""
                        publish_time = self._parse_publish_time(publish_time_str)

                        info_elem = item.query_selector('p.info')
                        summary = info_elem.get_attribute('title') if info_elem else ""
                        summary = summary.strip() if summary else ""

                        news_list.append({
                            "title": title,
                            "url": href,
                            "publish_time": publish_time,
                            "summary": summary,
                            "content": summary
                        })

                    except Exception as e:
                        continue

            except Exception as e:
                self.error_message = f"获取东方财富新闻失败: {str(e)}"
            finally:
                browser.close()

        return news_list

    async def fetch_news_list(self):
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(pool, self._fetch_sync)
        return result

    def _parse_publish_time(self, time_str: str) -> datetime:
        try:
            if not time_str:
                return datetime.now()

            time_str = time_str.strip()

            match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})', time_str)
            if match:
                year, month, day, hour, minute = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5))
                return datetime(year, month, day, hour, minute)

            match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', time_str)
            if match:
                year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                return datetime(year, month, day)

            return datetime.now()
        except:
            return datetime.now()

    def parse_news_item(self, raw_data):
        try:
            title = raw_data.get("title", "")
            content = raw_data.get("content", raw_data.get("summary", ""))
            url = raw_data.get("url", "")
            publish_time = raw_data.get("publish_time", datetime.now())

            if not title:
                return None

            return NewsItem(
                title=title,
                content=content,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                summary=raw_data.get("summary"),
                image_url=None
            )
        except Exception as e:
            self.error_message = f"解析失败: {str(e)}"
            return None

    def is_within_time_range(self, publish_time: datetime) -> bool:
        return True

    async def fetch_news_detail(self, url: str) -> dict:
        return {}

    async def crawl(self):
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            raw_news_list = await self.fetch_news_list()

            for raw_news in raw_news_list[:10]:
                news_item = self.parse_news_item(raw_news)

                if news_item:
                    self.news_list.append(news_item)
                    random_delay(min_delay=1, max_delay=2)

                    if len(self.news_list) >= 10:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list