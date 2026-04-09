import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from app.config import settings
from app.utils.anti_crawl import random_delay, get_random_headers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    content: str
    source: str
    publish_time: datetime
    url: str
    author: Optional[str] = None
    summary: Optional[str] = None
    image_url: Optional[str] = None
    news_type: Optional[str] = None


class BaseCrawler(ABC):
    source_name: str = ""
    
    def __init__(self):
        self.news_list: List[NewsItem] = []
        self.start_time: Optional[float] = None
        self.error_message: Optional[str] = None
    
    @abstractmethod
    async def fetch_news_list(self) -> List[Dict[str, Any]]:
        pass
    
    @abstractmethod
    async def fetch_news_detail(self, url: str) -> Optional[Dict[str, Any]]:
        pass
    
    @abstractmethod
    def parse_news_item(self, raw_data: Dict[str, Any]) -> Optional[NewsItem]:
        pass
    
    def is_within_time_range(self, publish_time: datetime) -> bool:
        cutoff = datetime.now() - timedelta(hours=settings.NEWS_TIME_RANGE_HOURS)
        return publish_time >= cutoff
    
    async def crawl(self) -> List[NewsItem]:
        self.start_time = time.time()
        self.news_list = []
        self.error_message = None
        
        logger.info(f"[{self.source_name}] 开始抓取...")
        
        try:
            logger.info(f"[{self.source_name}] 正在获取新闻列表...")
            raw_news_list = await self.fetch_news_list()
            logger.info(f"[{self.source_name}] 获取到 {len(raw_news_list)} 条原始新闻")
            
            raw_news_list = raw_news_list[:settings.NEWS_PER_SOURCE]
            logger.info(f"[{self.source_name}] 将处理前 {len(raw_news_list)} 条新闻")
            
            for idx, raw_news in enumerate(raw_news_list):
                logger.info(f"[{self.source_name}] 处理第 {idx+1}/{len(raw_news_list)} 条新闻")
                random_delay()
                news_item = self.parse_news_item(raw_news)
                
                if news_item:
                    logger.info(f"[{self.source_name}] 解析成功: {news_item.title[:50]}...")
                    if self.is_within_time_range(news_item.publish_time):
                        self.news_list.append(news_item)
                        logger.info(f"[{self.source_name}] 新闻已添加到列表 (当前: {len(self.news_list)})")
                    else:
                        logger.info(f"[{self.source_name}] 新闻超出时间范围，跳过")
                else:
                    logger.warning(f"[{self.source_name}] 新闻解析失败")
                    
                if len(self.news_list) >= settings.NEWS_PER_SOURCE:
                    logger.info(f"[{self.source_name}] 已达到最大抓取数量 {settings.NEWS_PER_SOURCE}，停止")
                    break
        
        except Exception as e:
            self.error_message = str(e)
            logger.error(f"[{self.source_name}] 抓取出错: {e}", exc_info=True)
        
        logger.info(f"[{self.source_name}] 抓取完成，共获取 {len(self.news_list)} 条新闻")
        return self.news_list
    
    def get_crawl_duration(self) -> Optional[int]:
        if self.start_time:
            return int(time.time() - self.start_time)
        return None
    
    def get_status(self) -> str:
        return "failed" if self.error_message else "success"
