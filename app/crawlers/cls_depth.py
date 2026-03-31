import asyncio
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay


class CLSDepthCrawler(BaseCrawler):
    source_name = "财联社头条"

    def __init__(self):
        super().__init__()
        self.base_url = "https://www.cls.cn"

    async def fetch_news_list(self):
        news_list = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN"
            )
            page = await context.new_page()

            try:
                print("访问财联社深度页面...")
                await page.goto(f"{self.base_url}/depth?id=1000", wait_until="networkidle", timeout=60000)
                await asyncio.sleep(3)

                articles = await page.query_selector_all('[class*="depth"], .article-item')

                print(f"找到 {len(articles)} 个文章元素")

                seen_titles = set()
                for i, article in enumerate(articles[:20]):
                    try:
                        title_elem = await article.query_selector('h3, .title, [class*="title"], a')
                        if not title_elem:
                            print(f"  第{i}条: 无标题元素，跳过")
                            continue

                        title = await title_elem.inner_text()
                        title = title.strip()

                        if not title or len(title) < 10:
                            print(f"  第{i}条: 标题太短或为空 '{title[:30] if title else ''}...'，跳过")
                            continue

                        if title in seen_titles:
                            print(f"  第{i}条: 重复标题 '{title[:30]}...'，跳过")
                            continue
                        seen_titles.add(title)

                        href = await title_elem.get_attribute('href')
                        if href:
                            if not href.startswith('http'):
                                href = self.base_url + href

                        time_elem = await article.query_selector('.time, [class*="time"]')
                        publish_time_str = await time_elem.inner_text() if time_elem else ""

                        publish_time = self._parse_publish_time(publish_time_str)

                        summary_elem = await article.query_selector('.desc, .summary, [class*="desc"], [class*="summary"]')
                        summary = await summary_elem.inner_text() if summary_elem else ""
                        summary = summary.strip() if summary else ""

                        if title:
                            news_list.append({
                                "title": title,
                                "url": href or "",
                                "publish_time": publish_time,
                                "summary": summary,
                                "content": summary
                            })
                            print(f"  ✅ {len(news_list)}. {title[:50]}...")

                    except Exception as e:
                        print(f"  第{i}条: 解析失败 - {e}")
                        continue

            except Exception as e:
                self.error_message = f"获取财联社头条失败: {str(e)}"
            finally:
                await browser.close()

        return news_list

    def _parse_publish_time(self, time_str: str) -> datetime:
        try:
            if not time_str:
                return datetime.now()

            time_str = time_str.strip()

            import re
            match = re.search(r'(\d{1,2})/(\d{1,2})', time_str)
            if match:
                month, day = int(match.group(1)), int(match.group(2))
                now = datetime.now()
                return datetime(now.year, month, day)

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

    async def fetch_news_detail(self, url: str) -> dict:
        return {}

    async def crawl(self):
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            raw_news_list = await self.fetch_news_list()

            for raw_news in raw_news_list[:5]:
                news_item = self.parse_news_item(raw_news)

                if news_item:
                    self.news_list.append(news_item)
                    await random_delay(min_delay=1, max_delay=2)

                    if len(self.news_list) >= 5:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list