from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class NewsBase(BaseModel):
    title: str
    content: Optional[str] = None
    source: str
    publish_time: datetime
    url: str
    author: Optional[str] = None
    summary: Optional[str] = None
    image_path: Optional[str] = None


class NewsCreate(NewsBase):
    pass


class NewsUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    publish_time: Optional[datetime] = None
    url: Optional[str] = None
    author: Optional[str] = None
    summary: Optional[str] = None
    image_path: Optional[str] = None


class News(NewsBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FilterRuleBase(BaseModel):
    include_keywords: Optional[str] = None
    exclude_keywords: Optional[str] = None


class FilterRuleCreate(FilterRuleBase):
    pass


class FilterRuleUpdate(FilterRuleBase):
    pass


class FilterRule(FilterRuleBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CrawlLogBase(BaseModel):
    source: str
    news_count: int
    status: str
    error_message: Optional[str] = None
    duration: Optional[int] = None


class CrawlLogCreate(CrawlLogBase):
    pass


class CrawlLogUpdate(BaseModel):
    source: Optional[str] = None
    news_count: Optional[int] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    duration: Optional[int] = None


class CrawlLog(CrawlLogBase):
    id: int
    crawl_time: datetime

    model_config = ConfigDict(from_attributes=True)


class IndexHighBase(BaseModel):
    symbol: str
    high_price: float


class IndexHighCreate(IndexHighBase):
    pass


class IndexHighUpdate(BaseModel):
    high_price: Optional[float] = None


class IndexHigh(IndexHighBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
