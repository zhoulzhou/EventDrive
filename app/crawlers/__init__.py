from .base import BaseCrawler, NewsItem
from .eastmoney_depth import EastmoneyDepthCrawler

# 新闻源只保留东方财富（2026-10-24）：同花顺（ths.py）、新浪财经（sina.py）已下线删除。
# 需要恢复时按 base.BaseCrawler 重新实现对应 source_name 即可，编排侧只需在
# scheduler.full_crawl 的 news_sources 里追加类。
__all__ = [
    "BaseCrawler",
    "NewsItem",
    "EastmoneyDepthCrawler",
]
