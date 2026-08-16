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


def get_filter_rule(db: Session, rule_id: int) -> Optional[models.FilterRule]:
    return db.query(models.FilterRule).filter(models.FilterRule.id == rule_id).first()


def get_latest_filter_rule(db: Session) -> Optional[models.FilterRule]:
    return db.query(models.FilterRule).order_by(desc(models.FilterRule.id)).first()


def create_filter_rule(db: Session, rule: schemas.FilterRuleCreate) -> models.FilterRule:
    db_rule = models.FilterRule(**rule.model_dump())
    db.add(db_rule)
    db.commit()
    db.refresh(db_rule)
    return db_rule


def update_filter_rule(db: Session, rule_id: int, rule: schemas.FilterRuleUpdate) -> Optional[models.FilterRule]:
    db_rule = get_filter_rule(db, rule_id)
    if db_rule:
        update_data = rule.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_rule, key, value)
        db.commit()
        db.refresh(db_rule)
    return db_rule


def delete_filter_rule(db: Session, rule_id: int) -> bool:
    db_rule = get_filter_rule(db, rule_id)
    if db_rule:
        db.delete(db_rule)
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
