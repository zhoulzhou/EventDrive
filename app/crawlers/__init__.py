from .base import BaseCrawler, NewsItem
from .cls import CLSCrawler
from .eastmoney import EastmoneyCrawler
from .kr36 import Kr36Crawler
from .cninfo import CninfoCrawler

__all__ = [
    "BaseCrawler",
    "NewsItem",
    "CLSCrawler",
    "EastmoneyCrawler",
    "Kr36Crawler",
    "CninfoCrawler"
]
