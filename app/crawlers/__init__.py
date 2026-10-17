from .base import BaseCrawler, NewsItem
from .eastmoney_depth import EastmoneyDepthCrawler
from .ths import THSNewsCrawler
from .sina import SinaNewsCrawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "EastmoneyDepthCrawler",
    "THSNewsCrawler",
    "SinaNewsCrawler",
]