import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import get_random_headers, random_delay


class CLSCrawler(BaseCrawler):
    source_name = "财联社"
    
    def __init__(self):
        super().__init__()
        self.base_url = "https://www.cls.cn"
        self.telegraph_api = "https://www.cls.cn/nodeapi/telegraphList"
    
    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        headers = get_random_headers(referer=self.base_url)
        news_list = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                params = {
                    "app": "CailianpressWeb",
                    "os": "web",
                    "rn": 50,
                    "sv": "7.7.5"
                }

                response = await client.get(
                    self.telegraph_api,
                    headers=headers,
                    params=params
                )
                response.raise_for_status()
                data = response.json()

                if data.get("error") == 0 and "data" in data:
                    roll_data = data["data"].get("roll_data", [])
                    news_list.extend(roll_data)

            except Exception as e:
                self.error_message = f"获取财联社新闻列表失败: {str(e)}"

        return news_list
    
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = get_random_headers(referer=self.base_url)
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return {"html": response.text}
            except Exception as e:
                self.error_message = f"获取财联社新闻详情失败: {str(e)}"
                return None
    
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            title = raw_data.get("title", "")
            content = raw_data.get("content", "")
            brief = raw_data.get("brief", "")

            if not title or not content:
                return None

            time_str = str(raw_data.get("ctime", ""))
            publish_time = self._parse_time(time_str)

            if not publish_time:
                return None

            news_id = raw_data.get("id", "")
            url = raw_data.get("shareurl", f"{self.base_url}/telegraph/{news_id}")

            image_url = raw_data.get("image", "") or raw_data.get("images", [""])[0] if raw_data.get("images") else ""

            return NewsItem(
                title=title,
                content=content,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                summary=brief if brief else None,
                image_url=image_url if image_url else None
            )

        except Exception as e:
            self.error_message = f"解析财联社新闻失败: {str(e)}"
            return None
    
    def _parse_time(self, time_str: str) -> Optional[datetime]:
        if not time_str:
            return None
        
        try:
            if isinstance(time_str, str):
                if len(time_str) == 10 and time_str.isdigit():
                    return datetime.fromtimestamp(int(time_str))
                elif len(time_str) == 13 and time_str.isdigit():
                    return datetime.fromtimestamp(int(time_str) / 1000)
            
            return datetime.now()
            
        except Exception:
            return None
