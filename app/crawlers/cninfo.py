import httpx
import brotli
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from app.crawlers.base import BaseCrawler, NewsItem
from app.utils.anti_crawl import random_delay, get_random_headers


class CninfoCrawler(BaseCrawler):
    source_name = "巨潮资讯"

    def __init__(self):
        super().__init__()
        self.base_url = "http://www.cninfo.com.cn"
        self.api_url = f"{self.base_url}/new/hisAnnouncement/query"
        self.detail_url = f"{self.base_url}/new/disclosure/detail"

    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        raw_news_list = []
        headers = get_random_headers()
        headers["Accept-Encoding"] = "gzip, deflate"

        end_date = datetime.now()
        start_date = end_date - timedelta(days=1)

        payload = {
            "pageNum": 1,
            "pageSize": 30,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "szse",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start_date.strftime('%Y-%m-%d')}~{end_date.strftime('%Y-%m-%d')}",
            "sortName": "time",
            "sortType": "desc",
            "isHLtitle": "true"
        }

        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            try:
                response = await client.post(self.api_url, headers=headers, data=payload)
                if response.status_code == 200:
                    content = response.content
                    if response.headers.get("Content-Encoding") == "br":
                        content = brotli.decompress(content)
                    data = response.json()
                    if data.get("announcements"):
                        raw_news_list = data["announcements"]
            except Exception as e:
                self.error_message = f"获取公告列表失败: {str(e)}"

        return raw_news_list
    
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        headers = get_random_headers(referer=self.base_url)
        
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    return {"html": response.text, "url": url}
            except Exception as e:
                self.error_message = f"获取公告详情失败: {str(e)}"
        
        return None
    
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        try:
            title = raw_data.get("announcementTitle", "")
            announcement_id = raw_data.get("announcementId", "")
            org_id = raw_data.get("orgId", "")
            announcement_time = raw_data.get("announcementTime", "")
            
            if not title or not announcement_id or not org_id or not announcement_time:
                return None
            
            publish_time = datetime.fromtimestamp(int(announcement_time) / 1000)
            url = f"{self.detail_url}?plate=szse&orgId={org_id}&announcementId={announcement_id}&announcementTime={publish_time.strftime('%Y-%m-%d')}"
            
            content = title
            author = raw_data.get("secName", "")
            summary = title[:200] if len(title) > 200 else title
            
            return NewsItem(
                title=title,
                content=content,
                source=self.source_name,
                publish_time=publish_time,
                url=url,
                author=author,
                summary=summary,
                image_url=None
            )
        except Exception as e:
            self.error_message = f"解析公告数据失败: {str(e)}"
            return None
