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


class MarketPriceBase(BaseModel):
    symbol: str
    name: str
    unit: Optional[str] = None
    value: Optional[float] = None
    date: Optional[str] = None


class MarketPriceCreate(MarketPriceBase):
    pass


class MarketPrice(BaseModel):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CompanyValuationBase(BaseModel):
    company_name: str
    base_profit: Optional[float] = None
    forecast_years: Optional[int] = None
    growth_forecast: Optional[float] = None
    growth_perpetual: Optional[float] = None
    discount_rate: Optional[float] = None
    enterprise_value: Optional[float] = None


class CompanyValuationCreate(CompanyValuationBase):
    pass


class CompanyValuation(CompanyValuationBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
