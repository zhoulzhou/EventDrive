from .base import BaseCrawler, NewsItem
from .cls import CLSCrawler
from .cls_depth import CLSDepthCrawler
from .eastmoney import EastmoneyCrawler
from .kr36 import Kr36Crawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "CLSCrawler",
    "CLSDepthCrawler",
    "EastmoneyCrawler",
    "Kr36Crawler"
]
