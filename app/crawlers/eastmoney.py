import re
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import httpx
from bs4 import BeautifulSoup
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay, get_random_headers


class EastmoneyCrawler(BaseCrawler):
    source_name: str = "东方财富"

    def __init__(self):
        super().__init__()
        self.base_url = "https://www.eastmoney.com"
        self.api_url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        headers = get_random_headers()
        headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.eastmoney.com",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                response = await client.get(self.api_url, headers=headers)
                response.raise_for_status()

                text = response.text
                if text.startswith("var ajaxResult="):
                    text = text[len("var ajaxResult="):]

                import json
                data = json.loads(text)

                if data.get("rc") == 1 and data.get("LivesList"):
                    for item in data["LivesList"][:20]:
                        title = item.get("title", "")
                        url = item.get("url_w", "") or item.get("url_m", "")
                        digest = item.get("digest", "")
                        simtitle = item.get("simtitle", "")

                        if title and url:
                            raw_news_list.append({
                                "url": url,
                                "title": title,
                                "summary": digest or simtitle,
                                "source": "东方财富"
                            })

            except Exception as e:
                self.error_message = f"获取东方财富新闻列表失败: {str(e)}"

        return raw_news_list
    
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = get_random_headers(referer=self.base_url)
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return {
                        'url': url,
                        'html': response.text
                    }
            except Exception as e:
                self.error_message = f"获取东方财富新闻详情失败 {url}: {str(e)}"
        return None
    
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            url = raw_data.get('url', '')
            title = raw_data.get('title', '')

            if not url or not title:
                return None

            publish_time = datetime.now() - timedelta(hours=1)
            content = raw_data.get('content', raw_data.get('summary', ''))
            summary = raw_data.get('summary', '')

            return NewsItem(
                title=title,
                content=content,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                author=None,
                summary=summary if summary else None,
                image_url=None
            )
        except Exception as e:
            self.error_message = f"解析东方财富新闻失败: {str(e)}"
            return None
    
    def _extract_content(self, soup: BeautifulSoup) -> str:
        content_divs = [
            '.content', '.article-content', '.txt-content', '#ContentBody',
            '[class*="content"]', 'article', '.main-text'
        ]
        
        for selector in content_divs:
            content_div = soup.select_one(selector)
            if content_div:
                paragraphs = content_div.find_all('p')
                if paragraphs:
                    content = '\n'.join([p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)])
                    if content:
                        return content
                else:
                    content = content_div.get_text(strip=True)
                    if content:
                        return content
        return ''
    
    def _extract_publish_time(self, soup: BeautifulSoup) -> datetime:
        time_selectors = ['.time', '.publish-time', '.date', '.pubtime', '[class*="time"]', '[class*="date"]']
        
        time_str = ''
        for selector in time_selectors:
            time_tag = soup.select_one(selector)
            if time_tag:
                time_str = time_tag.get_text(strip=True)
                if time_str:
                    break
        
        if time_str:
            publish_time = self._parse_publish_time(time_str)
            if publish_time:
                return publish_time
        
        return datetime.now() - timedelta(hours=1)
    
    def _parse_publish_time(self, time_str: str) -> Optional[datetime]:
        patterns = [
            r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})',
            r'(\d{4})-(\d{1,2})-(\d{1,2})\s*(\d{1,2}):(\d{1,2})',
            r'(\d{4})/(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{1,2})',
            r'(\d{4})年(\d{1,2})月(\d{1,2})日',
            r'(\d{4})-(\d{1,2})-(\d{1,2})',
            r'(\d{4})/(\d{1,2})/(\d{1,2})'
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
        return None
    
    def _extract_author(self, soup: BeautifulSoup) -> str:
        author_selectors = ['.author', '.source', '.from', '[class*="author"]', '[class*="source"]']
        
        for selector in author_selectors:
            author_tag = soup.select_one(selector)
            if author_tag:
                author = author_tag.get_text(strip=True)
                if author:
                    return author
        return ''
    
    def _generate_summary(self, content: str) -> str:
        if not content:
            return ''
        content = re.sub(r'\s+', ' ', content).strip()
        return content[:197] + '...' if len(content) > 200 else content
    
    def _extract_image_url(self, soup: BeautifulSoup) -> Optional[str]:
        img_selectors = ['.content img', '.article-content img', 'img[src*="eastmoney"]', 'article img']
        
        for selector in img_selectors:
            img_tag = soup.select_one(selector)
            if img_tag and img_tag.get('src'):
                image_url = img_tag.get('src')
                if image_url:
                    if not image_url.startswith('http'):
                        image_url = self.base_url + image_url if image_url.startswith('/') else self.base_url + '/' + image_url
                    return image_url
        return None
    
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
