from .base import BaseCrawler, NewsItem
from .cls_depth import CLSDepthCrawler
from .eastmoney_depth import EastmoneyDepthCrawler
from .kr36_depth import Kr36DepthCrawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "CLSDepthCrawler",
    "EastmoneyDepthCrawler",
    "Kr36DepthCrawler"
]