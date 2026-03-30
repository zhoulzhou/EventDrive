import json
import re
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional
import httpx
import aiohttp
from bs4 import BeautifulSoup
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import get_random_headers, random_delay


class Kr36Crawler(BaseCrawler):
    source_name = "36氪"

    def __init__(self):
        super().__init__()
        self.base_url = "https://36kr.com"
        self.rss_url = "https://36kr.com/feed"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        headers = get_random_headers()
        raw_news_list = []

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                response = await client.get(self.rss_url, headers=headers)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'xml')
                    items = soup.find_all('item')

                    for item in items[:20]:
                        try:
                            title = item.find('title')
                            link = item.find('link')
                            pub_date = item.find('pubDate')
                            description = item.find('description')

                            title_text = title.get_text(strip=True) if title else ""
                            link_text = link.get_text(strip=True) if link else ""
                            pub_date_text = pub_date.get_text(strip=True) if pub_date else ""
                            desc_text = description.get_text(strip=True) if description else ""

                            if title_text and link_text:
                                raw_news_list.append({
                                    "title": title_text,
                                    "url": link_text,
                                    "publish_time_str": pub_date_text,
                                    "summary": self._clean_html(desc_text)
                                })
                        except Exception:
                            continue

            except Exception as e:
                self.error_message = f"获取36氪新闻列表失败: {str(e)}"

        return raw_news_list

    def _clean_html(self, html_text: str) -> str:
        if not html_text:
            return ""
        clean_text = re.sub(r'<[^>]+>', '', html_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        return clean_text
    
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = get_random_headers(referer=self.base_url)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    return {"html": html, "url": url}
        
        return None
    
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            title = raw_data.get("title", "")
            url = raw_data.get("url", "")
            publish_time_str = raw_data.get("publish_time_str", "")
            summary = raw_data.get("summary", "")

            if not title or not url:
                return None

            publish_time = self._parse_publish_time(publish_time_str)

            return NewsItem(
                title=title,
                content="",
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                author=None,
                summary=summary,
                image_url=None
            )
        except Exception as e:
            return None

    def _parse_publish_time(self, time_str: str) -> datetime:
        try:
            if time_str:
                from email.utils import parsedate_to_datetime
                try:
                    return parsedate_to_datetime(time_str)
                except:
                    pass
        except Exception:
            pass

        return datetime.now()
    
    async def crawl(self) -> List[NewsItem]:
        self.start_time = asyncio.get_event_loop().time()
        self.news_list = []
        self.error_message = None

        try:
            raw_news_list = await self.fetch_news_list()

            for raw_news in raw_news_list:
                news_item = self.parse_news_item(raw_news)

                if news_item and self.is_within_time_range(news_item.publish_time):
                    self.news_list.append(news_item)
                    random_delay(min_delay=1, max_delay=3)

                    if len(self.news_list) >= 10:
                        break

        except Exception as e:
            self.error_message = str(e)

        return self.news_list
    
    def _extract_content_from_html(self, html: str) -> str:
        try:
            import re
            
            script_match = re.search(r'<script>window\.initialState\s*=\s*(\{.*?\});</script>', html, re.DOTALL)
            if script_match:
                json_str = script_match.group(1)
                try:
                    data = json.loads(json_str)
                    article_detail = data.get("articleDetail", {})
                    article_detail_data = article_detail.get("articleDetailData", {})
                    widget_content = article_detail_data.get("data", {}).get("widgetContent", "")
                    
                    if widget_content:
                        content_text = re.sub(r'<[^>]+>', '', widget_content)
                        content_text = re.sub(r'\s+', ' ', content_text).strip()
                        return content_text
                except:
                    pass
            
            return ""
        except Exception:
            return ""
