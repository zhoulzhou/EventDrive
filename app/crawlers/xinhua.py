import asyncio
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright, Browser, Page
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay, get_random_headers


class XinhuaCrawler(BaseCrawler):
    source_name: str = "新华网"
    
    def __init__(self):
        super().__init__()
        self.base_url = "https://www.xinhuanet.com"
        self.news_url = "https://www.xinhuanet.com/world"
    
    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            headers = get_random_headers()
            await page.set_extra_http_headers(headers)
            
            try:
                await page.goto(self.news_url, wait_until="networkidle", timeout=30000)
                random_delay()
                
                news_items = await page.query_selector_all("li.dataList, li.newsItem, div.news-item, a[href*='/politics/'], a[href*='/world/']")
                
                for idx, item in enumerate(news_items[:20]):
                    try:
                        href = await item.get_attribute("href")
                        if not href:
                            a_tag = await item.query_selector("a")
                            if a_tag:
                                href = await a_tag.get_attribute("href")
                        
                        if href:
                            if not href.startswith("http"):
                                href = self.base_url + href if href.startswith("/") else self.base_url + "/" + href
                            
                            title_elem = await item.query_selector("h3, h4, .title, span")
                            title = ""
                            if title_elem:
                                title = await title_elem.inner_text()
                            else:
                                title = await item.inner_text()
                            
                            title = title.strip()
                            if title and len(title) > 5:
                                raw_news_list.append({
                                    "url": href,
                                    "title": title
                                })
                    except Exception:
                        continue
            finally:
                await browser.close()
        
        return raw_news_list
    
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            headers = get_random_headers(referer=self.base_url)
            await page.set_extra_http_headers(headers)
            
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                random_delay()
                
                title = ""
                title_elem = await page.query_selector("h1, .title, h2")
                if title_elem:
                    title = await title_elem.inner_text()
                
                content = ""
                content_elems = await page.query_selector_all("div.content, article, div.main-content, p")
                for elem in content_elems:
                    text = await elem.inner_text()
                    if len(text) > 50:
                        content += text + "\n"
                
                publish_time_str = ""
                time_elem = await page.query_selector(".time, span.time, div[class*='time'], .pub-time")
                if time_elem:
                    publish_time_str = await time_elem.inner_text()
                
                author = ""
                author_elem = await page.query_selector(".author, span.author, div[class*='author']")
                if author_elem:
                    author = await author_elem.inner_text()
                
                image_url = ""
                img_elem = await page.query_selector("div.content img, article img, img[class*='main']")
                if img_elem:
                    image_url = await img_elem.get_attribute("src")
                    if image_url and not image_url.startswith("http"):
                        image_url = self.base_url + image_url if image_url.startswith("/") else self.base_url + "/" + image_url
                
                return {
                    "url": url,
                    "title": title,
                    "content": content.strip(),
                    "publish_time_str": publish_time_str,
                    "author": author.strip(),
                    "image_url": image_url
                }
            except Exception as e:
                return None
            finally:
                await browser.close()
    
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            url = raw_data.get("url", "")
            title = raw_data.get("title", "")
            
            if not url or not title:
                return None
            
            publish_time = self._parse_publish_time(raw_data.get("publish_time_str", ""))
            
            content = raw_data.get("content", "")
            summary = self._generate_summary(content)
            
            return NewsItem(
                title=title,
                content=content,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                author=raw_data.get("author"),
                summary=summary,
                image_url=raw_data.get("image_url")
            )
        except Exception:
            return None
    
    def _parse_publish_time(self, time_str: str) -> datetime:
        time_str = time_str.strip()
        
        patterns = [
            r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})",
            r"(\d{4})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{1,2})",
            r"(\d{4})/(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{1,2})",
            r"(\d{4})年(\d{1,2})月(\d{1,2})日",
            r"(\d{4})-(\d{1,2})-(\d{1,2})",
            r"(\d{4})/(\d{1,2})/(\d{1,2})"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, time_str)
            if match:
                groups = match.groups()
                try:
                    year = int(groups[0])
                    month = int(groups[1])
                    day = int(groups[2])
                    hour = int(groups[3]) if len(groups) > 3 else 0
                    minute = int(groups[4]) if len(groups) > 4 else 0
                    return datetime(year, month, day, hour, minute)
                except (ValueError, IndexError):
                    continue
        
        return datetime.now()
    
    def _generate_summary(self, content: str) -> str:
        if not content:
            return ""
        
        content = re.sub(r"\s+", " ", content).strip()
        if len(content) <= 200:
            return content
        return content[:197] + "..."
    
    async def crawl(self) -> List[NewsItem]:
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None
        
        try:
            raw_news_list = await self.fetch_news_list()
            raw_news_list = raw_news_list[:20]
            
            for raw_news in raw_news_list:
                random_delay()
                
                detail_data = await self.fetch_news_detail(raw_news["url"])
                if detail_data:
                    detail_data["title"] = raw_news.get("title", detail_data.get("title", ""))
                    news_item = self.parse_news_item(detail_data)
                    
                    if news_item and self.is_within_time_range(news_item.publish_time):
                        self.news_list.append(news_item)
                        
                        if len(self.news_list) >= 10:
                            break
        except Exception as e:
            self.error_message = str(e)
        
        return self.news_list
