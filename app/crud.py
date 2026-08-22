from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from app import models, schemas


def get_news(db: Session, news_id: int) -> Optional[models.News]:
    return db.query(models.News).filter(models.News.id == news_id).first()


def get_news_by_url(db: Session, url: str) -> Optional[models.News]:
    return db.query(models.News).filter(models.News.url == url).first()


def is_news_exists(db: Session, url: str) -> bool:
    return get_news_by_url(db, url) is not None


def get_news_list(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    source: Optional[str] = None,
    include_keywords: Optional[List[str]] = None,
    exclude_keywords: Optional[List[str]] = None
) -> List[models.News]:
    query = db.query(models.News)
    
    cutoff_time = datetime.now() - timedelta(hours=24)
    query = query.filter(models.News.publish_time >= cutoff_time)
    
    if source:
        query = query.filter(models.News.source == source)
    
    if include_keywords and include_keywords:
        keyword_filters = []
        for keyword in include_keywords:
            keyword = keyword.strip()
            if keyword:
                keyword_filters.append(models.News.title.contains(keyword))
                keyword_filters.append(models.News.content.contains(keyword))
        if keyword_filters:
            query = query.filter(or_(*keyword_filters))
    
    if exclude_keywords and exclude_keywords:
        for keyword in exclude_keywords:
            keyword = keyword.strip()
            if keyword:
                query = query.filter(
                    ~models.News.title.contains(keyword),
                    ~models.News.content.contains(keyword)
                )
    
    query = query.order_by(desc(models.News.publish_time))
    
    return query.offset(skip).limit(limit).all()


def create_news(db: Session, news: schemas.NewsCreate) -> models.News:
    db_news = models.News(**news.model_dump())
    db.add(db_news)
    db.commit()
    db.refresh(db_news)
    return db_news


def update_news(db: Session, news_id: int, news: schemas.NewsUpdate) -> Optional[models.News]:
    db_news = get_news(db, news_id)
    if db_news:
        update_data = news.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_news, key, value)
        db.commit()
        db.refresh(db_news)
    return db_news


def delete_news(db: Session, news_id: int) -> bool:
    db_news = get_news(db, news_id)
    if db_news:
        db.delete(db_news)
        db.commit()
        return True
    return False


def get_crawl_log(db: Session, log_id: int) -> Optional[models.CrawlLog]:
    return db.query(models.CrawlLog).filter(models.CrawlLog.id == log_id).first()


def get_crawl_logs(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    source: Optional[str] = None
) -> List[models.CrawlLog]:
    query = db.query(models.CrawlLog)
    
    if source:
        query = query.filter(models.CrawlLog.source == source)
    
    query = query.order_by(desc(models.CrawlLog.crawl_time))
    
    return query.offset(skip).limit(limit).all()


def get_latest_crawl_log(db: Session, source: Optional[str] = None) -> Optional[models.CrawlLog]:
    query = db.query(models.CrawlLog)
    if source:
        query = query.filter(models.CrawlLog.source == source)
    return query.order_by(desc(models.CrawlLog.crawl_time)).first()


def create_crawl_log(db: Session, log: schemas.CrawlLogCreate) -> models.CrawlLog:
    db_log = models.CrawlLog(**log.model_dump())
    db.add(db_log)
    db.commit()
    db.refresh(db_log)
    return db_log


def update_crawl_log(db: Session, log_id: int, log: schemas.CrawlLogUpdate) -> Optional[models.CrawlLog]:
    db_log = get_crawl_log(db, log_id)
    if db_log:
        update_data = log.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_log, key, value)
        db.commit()
        db.refresh(db_log)
    return db_log


def delete_crawl_log(db: Session, log_id: int) -> bool:
    db_log = get_crawl_log(db, log_id)
    if db_log:
        db.delete(db_log)
        db.commit()
        return True
    return False


def get_all_market_prices(db: Session) -> List[models.MarketPrice]:
    return db.query(models.MarketPrice).all()


def upsert_market_price(db: Session, price: schemas.MarketPriceCreate) -> models.MarketPrice:
    """按 (symbol, date) 更新或插入一条历史记录。"""
    db_price = (
        db.query(models.MarketPrice)
        .filter(models.MarketPrice.symbol == price.symbol, models.MarketPrice.date == price.date)
        .first()
    )
    if db_price:
        db_price.name = price.name
        db_price.unit = price.unit
        db_price.value = price.value
    else:
        db_price = models.MarketPrice(**price.model_dump())
        db.add(db_price)
    db.commit()
    db.refresh(db_price)
    return db_price


def get_latest_market_prices(db: Session) -> Dict[str, List[models.MarketPrice]]:
    """返回每个 symbol 最新两条记录（用于显示最新值并计算涨跌幅）。"""
    symbols = [r[0] for r in db.query(models.MarketPrice.symbol).distinct().all()]
    result: Dict[str, List[models.MarketPrice]] = {}
    for sym in symbols:
        rows = (
            db.query(models.MarketPrice)
            .filter(models.MarketPrice.symbol == sym)
            .order_by(models.MarketPrice.date.desc())
            .limit(2)
            .all()
        )
        if rows:
            result[sym] = rows
    return result


def get_market_history(db: Session, symbol: str) -> List[models.MarketPrice]:
    """返回某个 symbol 的全部历史记录（按日期升序）。"""
    return (
        db.query(models.MarketPrice)
        .filter(models.MarketPrice.symbol == symbol)
        .order_by(models.MarketPrice.date.asc())
        .all()
    )


def get_market_peak(db: Session, symbol: str) -> Optional[models.MarketPrice]:
    """返回某 symbol 历史最大值记录（作为峰值，用于回撤预警）。"""
    return (
        db.query(models.MarketPrice)
        .filter(models.MarketPrice.symbol == symbol)
        .order_by(models.MarketPrice.value.desc())
        .first()
    )


def get_market_strategy_state(db: Session, symbol: str) -> Optional[models.MarketStrategyState]:
    """读取某 symbol 的峰值回撤策略状态（当前峰值/峰值日期/最近回撤触发日）。"""
    return (
        db.query(models.MarketStrategyState)
        .filter(models.MarketStrategyState.symbol == symbol)
        .first()
    )


def save_market_strategy_state(
    db: Session, symbol: str, peak_value: float, peak_date: str, drawdown_date: Optional[str] = None
) -> models.MarketStrategyState:
    """写入（新增或更新）某 symbol 的峰值回撤策略状态。"""
    state = get_market_strategy_state(db, symbol)
    if state:
        state.peak_value = peak_value
        state.peak_date = peak_date
        state.drawdown_date = drawdown_date
    else:
        state = models.MarketStrategyState(
            symbol=symbol, peak_value=peak_value, peak_date=peak_date, drawdown_date=drawdown_date
        )
        db.add(state)
    db.commit()
    db.refresh(state)
    return state


# index/ 下的 CSV 文件名 -> IndexHistory 表列名（列名取 CSV 文件名，与市场行情字段分离）
INDEX_CSV_COLUMNS = {
    "NASDAQCOM_2015.csv": "NASDAQCOM_2015",
    "VIXCLS_2015.csv": "VIXCLS_2015",
    "DGS2_2015.csv": "DGS2_2015",
    "DGS10_2015.csv": "DGS10_2015",
}
INDEX_COLUMN_NAMES = [
    "NASDAQCOM_2015",
    "VIXCLS_2015",
    "DGS2_2015",
    "DGS10_2015",
]


def get_index_history_all(db: Session) -> List[models.IndexHistory]:
    """返回指数预警全部历史记录（按日期升序）。"""
    return (
        db.query(models.IndexHistory)
        .order_by(models.IndexHistory.date.asc())
        .all()
    )


def get_index_history_count(db: Session) -> int:
    return db.query(models.IndexHistory).count()


def delete_all_index_history(db: Session) -> int:
    """清空指数预警历史表（CSV 更新后强制重导时使用）。"""
    deleted = db.query(models.IndexHistory).delete()
    db.commit()
    return deleted


def upsert_index_history_rows(db: Session, rows: List[dict]) -> int:
    """按 date 更新或插入多条指数预警历史记录。rows 形如 [{date, column, value}]。"""
    seen: Dict[str, models.IndexHistory] = {}
    for row in rows:
        rec = seen.get(row["date"])
        if rec is None:
            rec = db.query(models.IndexHistory).filter(models.IndexHistory.date == row["date"]).first()
            if rec is None:
                rec = models.IndexHistory(date=row["date"])
                db.add(rec)
            seen[row["date"]] = rec
        setattr(rec, row["column"], row["value"])
    db.commit()
    return len(seen)


# 财务指标 显示名 -> FinancialReport 表列名（顺序即展示顺序）
FINANCIAL_FIELD_COLUMNS = {
    "营业收入": "revenue",
    "营业成本": "operating_cost",
    "归母净利润": "net_profit",
    "存货": "inventory",
    "应收账款": "accounts_receivable",
    "货币资金": "cash",
    "短期理财": "short_term_investment",
    "合同负债": "contract_liabilities",
    "股东权益": "shareholders_equity",
    "经营活动现金流净额": "operating_cash_flow",
    "短期借款": "short_term_borrowing",
    "一年内到期的非流动负债": "non_current_liab_due_1y",
    "长期借款": "long_term_borrowing",
    "应付债券": "bonds_payable",
    "利息支出": "interest_expense",
}


def upsert_financial_reports(
    db: Session,
    stock_code: str,
    records: List[dict],
    stock_name: Optional[str] = None,
) -> int:
    """按 (stock_code, report_date) 更新或插入多条财务指标记录。

    records 形如 [{"报告期": "2024-06-30", "营业收入": 123.0, ...}]，值需为 float/None。
    """
    count = 0
    for rec in records:
        db_record = (
            db.query(models.FinancialReport)
            .filter(
                models.FinancialReport.stock_code == stock_code,
                models.FinancialReport.report_date == rec["报告期"],
            )
            .first()
        )
        if db_record is None:
            db_record = models.FinancialReport(
                stock_code=stock_code, report_date=rec["报告期"]
            )
            db.add(db_record)
        if stock_name:
            db_record.stock_name = stock_name
        for display_name, column in FINANCIAL_FIELD_COLUMNS.items():
            setattr(db_record, column, rec.get(display_name))
        count += 1
    db.commit()
    return count


def get_financial_reports(
    db: Session,
    stock_code: Optional[str] = None,
    stock_name: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> List[models.FinancialReport]:
    """查询财务指标记录（按报告期升序）。

    stock_code / stock_name 至少提供一个作为过滤条件；start/end 为报告期范围。
    """
    query = db.query(models.FinancialReport)
    if stock_code:
        query = query.filter(models.FinancialReport.stock_code == stock_code)
    if stock_name:
        query = query.filter(models.FinancialReport.stock_name == stock_name)
    if start:
        query = query.filter(models.FinancialReport.report_date >= start)
    if end:
        query = query.filter(models.FinancialReport.report_date <= end)
    return query.order_by(models.FinancialReport.report_date.asc()).all()
