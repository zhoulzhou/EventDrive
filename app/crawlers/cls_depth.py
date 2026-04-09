import asyncio
from datetime import datetime
from pathlib import Path
import sys
import re
import concurrent.futures
import logging
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay

logger = logging.getLogger(__name__)


class CLSDepthCrawler(BaseCrawler):
    source_name = "财联社头条"

    def __init__(self):
        super().__init__()
        self.base_url = "https://www.cls.cn"

    def _fetch_sync(self):
        news_list = []
        logger.info(f"[财联社] 开始抓取，URL: {self.base_url}/depth?id=1000")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN"
            )
            page = context.new_page()

            try:
                logger.info(f"[财联社] 正在访问页面...")
                page.goto(f"{self.base_url}/depth?id=1000", wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                logger.info(f"[财联社] 页面加载完成，当前URL: {page.url}")

                articles = page.query_selector_all('[class*="depth"], .article-item')
                logger.info(f"[财联社] 找到 {len(articles)} 个文章元素")

                if len(articles) == 0:
                    logger.warning(f"[财联社] 未找到任何文章元素，尝试获取页面HTML片段...")
                    page_content = page.content()
                    logger.info(f"[财联社] 页面标题: {page.title()}")
                    logger.info(f"[财联社] 页面HTML前500字符: {page_content[:500]}")

                seen_urls = set()
                for idx, article in enumerate(articles):
                    try:
                        title_elem = article.query_selector('h3, .title, [class*="title"], a')
                        if not title_elem:
                            logger.debug(f"[财联社] 第{idx+1}个文章: 未找到标题元素")
                            continue

                        href = title_elem.get_attribute('href')
                        if not href:
                            logger.debug(f"[财联社] 第{idx+1}个文章: 无href属性")
                            continue
                        if not href.startswith('/detail/'):
                            logger.debug(f"[财联社] 第{idx+1}个文章: href不以/detail/开头: {href}")
                            continue

                        url = self.base_url + href

                        if url in seen_urls:
                            logger.debug(f"[财联社] 第{idx+1}个文章: URL已存在，跳过")
                            continue
                        seen_urls.add(url)

                        title = title_elem.inner_text()
                        title = title.strip()

                        if not title or title == "None" or len(title) < 5:
                            logger.debug(f"[财联社] 第{idx+1}个文章: 标题无效: '{title}'")
                            continue

                        time_elem = article.query_selector('.time, [class*="time"]')
                        publish_time_str = time_elem.inner_text() if time_elem else ""
                        publish_time = self._parse_publish_time(publish_time_str)

                        summary_elem = article.query_selector('.desc, .summary, [class*="desc"], [class*="summary"]')
                        summary = summary_elem.inner_text() if summary_elem else ""
                        summary = summary.strip() if summary else ""

                        logger.info(f"[财联社] 第{idx+1}个文章: 标题='{title[:30]}...', URL={url}")
                        news_list.append({
                            "title": title,
                            "url": url,
                            "publish_time": publish_time,
                            "summary": summary,
                            "content": summary
                        })

                    except Exception as e:
                        logger.error(f"[财联社] 第{idx+1}个文章解析异常: {e}")
                        continue

                logger.info(f"[财联社] 抓取完成，共获取 {len(news_list)} 条新闻")

            except Exception as e:
                self.error_message = f"获取财联社头条失败: {str(e)}"
                logger.error(f"[财联社] 页面访问异常: {e}")
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

    def is_within_time_range(self, publish_time: datetime) -> bool:
        return True

    async def fetch_news_detail(self, url: str) -> dict:
        return {}

    async def crawl(self):
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            logger.info(f"[财联社] 调用 fetch_news_list()...")
            raw_news_list = await self.fetch_news_list()
            logger.info(f"[财联社] fetch_news_list() 返回 {len(raw_news_list)} 条")

            for raw_news in raw_news_list[:5]:
                news_item = self.parse_news_item(raw_news)

                if news_item:
                    self.news_list.append(news_item)
                    random_delay(min_delay=1, max_delay=2)

                    if len(self.news_list) >= 5:
                        break

        except Exception as e:
            self.error_message = str(e)
            logger.error(f"[财联社] crawl() 异常: {e}", exc_info=True)

        return self.news_list