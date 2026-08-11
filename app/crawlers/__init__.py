from .base import BaseCrawler, NewsItem
from .eastmoney_depth import EastmoneyDepthCrawler
from .nytimes import NYTCrawler, NYTDepthCrawler
from .bbc import BBCCrawler
from .finnhub_index import FinnhubIndexCrawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "EastmoneyDepthCrawler",
    "NYTCrawler",
    "NYTDepthCrawler",
    "BBCCrawler",
    "FinnhubIndexCrawler"
]
