from .base import BaseCrawler, NewsItem
from .cls_depth import CLSDepthCrawler
from .eastmoney_depth import EastmoneyDepthCrawler
from .kr36_depth import Kr36DepthCrawler
from .nytimes import NYTCrawler, NYTDepthCrawler
from .apnews import APNewsCrawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "CLSDepthCrawler",
    "EastmoneyDepthCrawler",
    "Kr36DepthCrawler",
    "NYTCrawler",
    "NYTDepthCrawler",
    "APNewsCrawler"
]