from sqlalchemy import Column, Integer, String, Text, DateTime, Float, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class News(Base):
    __tablename__ = "news"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    source = Column(Text, nullable=False)
    publish_time = Column(DateTime, nullable=False)
    url = Column(Text, unique=True, nullable=False)
    author = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    image_path = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class FilterRule(Base):
    __tablename__ = "filter_rules"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    include_keywords = Column(Text, nullable=True)
    exclude_keywords = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    source = Column(Text, nullable=False)
    crawl_time = Column(DateTime, nullable=False, server_default=func.now())
    news_count = Column(Integer, nullable=False)
    status = Column(Text, nullable=False)
    error_message = Column(Text, nullable=True)
    duration = Column(Integer, nullable=True)


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_market_symbol_date"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=False)
    unit = Column(Text, nullable=True)
    value = Column(Float, nullable=True)
    date = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class MarketStrategyState(Base):
    """市场行情峰值回撤策略状态：持久化每个指标的当前峰值，跨天/跨重启生效。

    - peak_value / peak_date: 当前追踪峰值（新高自动上移，回撤达阈值时重置为当前值）
    - drawdown_date: 最近一次触发回撤预警（-red_pct%）的日期，用于该日显示红色预警
    """
    __tablename__ = "market_strategy_state"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(Text, nullable=False, unique=True, index=True)
    peak_value = Column(Float, nullable=False)
    peak_date = Column(Text, nullable=False)
    drawdown_date = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class IndexHistory(Base):
    """指数预警历史宽表：一行一个日期，每列对应 index/ 下一个 CSV 文件（列名取 CSV 文件名）。"""
    __tablename__ = "index_history"
    __table_args__ = (UniqueConstraint("date", name="uq_index_history_date"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(Text, nullable=False, index=True)
    NASDAQCOM_2015 = Column(Float, nullable=True)
    VIXCLS_2015 = Column(Float, nullable=True)
    DGS2_2015 = Column(Float, nullable=True)
    DGS10_2015 = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
