import json
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_

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


def cleanup_sparse_market_dates(db: Session, symbols: List[str], limit: int = 30) -> int:
    """删除"最近 N 个日期内、当日非空值数量 < 3"的行，保证历史曲线断点少、时间轴对齐。

    规则：对给定 symbols（默认市场行情 4 指标），先取这些指标最近 limit 个日期的记录，
    只在这批日期内统计每一天有非空值的指标个数，若某日不足 3 个指标有值，
    则删除该日所有属于这些 symbol 的行。返回删除条数。
    """
    import logging
    logger = logging.getLogger(__name__)

    # 取这些指标最近 limit 个日期（去重后按日期倒序取前 limit 个）
    recent_dates = [
        d[0]
        for d in db.query(models.MarketPrice.date)
        .filter(models.MarketPrice.symbol.in_(symbols))
        .order_by(models.MarketPrice.date.desc())
        .limit(limit)
        .all()
    ]
    if not recent_dates:
        return 0

    # 在最近这批日期中统计各 symbol 的非空数，找出不足 3 的日期
    sparse_dates = [
        row[0]
        for row in db.query(models.MarketPrice.date)
        .filter(
            models.MarketPrice.symbol.in_(symbols),
            models.MarketPrice.date.in_(recent_dates),
        )
        .group_by(models.MarketPrice.date)
        .having(func.count(models.MarketPrice.value) < 3)
        .all()
    ]
    if not sparse_dates:
        return 0

    deleted = (
        db.query(models.MarketPrice)
        .filter(
            models.MarketPrice.symbol.in_(symbols),
            models.MarketPrice.date.in_(sparse_dates),
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info(f"清理稀疏日期数据: 删除 {deleted} 行 (日期: {sparse_dates}, 非空指标数<3)")
    return deleted


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


def list_financial_stocks(db: Session) -> List[dict]:
    """列出已入库财务数据的所有股票（含代码，按名称去重排序），供前端下拉选择。"""
    rows = (
        db.query(models.FinancialReport.stock_name, models.FinancialReport.stock_code)
        .filter(models.FinancialReport.stock_name.isnot(None))
        .distinct()
        .order_by(models.FinancialReport.stock_name.asc())
        .all()
    )
    return [{"name": name, "code": code} for name, code in rows if name]


def get_valuation_records(db: Session, limit: int = 100) -> List[models.CompanyValuation]:
    """查询最近 limit 条估值记录（按时间倒序，最新在前）。"""
    return (
        db.query(models.CompanyValuation)
        .order_by(desc(models.CompanyValuation.created_at))
        .limit(limit)
        .all()
    )


def create_valuation_record(
    db: Session, valuation: schemas.CompanyValuationCreate
) -> models.CompanyValuation:
    """新增一条估值记录，并裁剪历史至最近 100 条。"""
    record = models.CompanyValuation(**valuation.model_dump())
    db.add(record)
    db.flush()
    # 裁剪：仅保留最近 100 条（按 id 倒序）
    keep_ids = [
        row[0]
        for row in db.query(models.CompanyValuation.id)
        .order_by(desc(models.CompanyValuation.id))
        .limit(100)
        .all()
    ]
    if keep_ids:
        db.query(models.CompanyValuation).filter(
            ~models.CompanyValuation.id.in_(keep_ids)
        ).delete(synchronize_session=False)
    db.commit()
    db.refresh(record)
    return record


def delete_valuation_record(db: Session, record_id: int) -> bool:
    """按 id 删除一条估值记录。"""
    record = (
        db.query(models.CompanyValuation)
        .filter(models.CompanyValuation.id == record_id)
        .first()
    )
    if record:
        db.delete(record)
        db.commit()
        return True
    return False


def clear_valuation_records(db: Session) -> int:
    """清空所有估值记录，返回删除条数。"""
    deleted = db.query(models.CompanyValuation).delete()
    db.commit()
    return deleted


# ---------------------------------------------------------------- 市场指标

def get_macro_indicators(db: Session) -> Dict[str, models.MacroIndicator]:
    """返回全部市场指标最新读数，按 key 索引。"""
    rows = db.query(models.MacroIndicator).all()
    return {row.key: row for row in rows}


def get_macro_indicator(db: Session, key: str) -> Optional[models.MacroIndicator]:
    return db.query(models.MacroIndicator).filter(models.MacroIndicator.key == key).first()


def upsert_macro_indicator(
    db: Session,
    key: str,
    value: Optional[float],
    as_of: Optional[str],
    source: str,
    detail: str = "{}",
) -> models.MacroIndicator:
    """按 key 写入（新增或更新）一条市场指标读数。"""
    row = get_macro_indicator(db, key)
    if row is None:
        row = models.MacroIndicator(key=key)
        db.add(row)
    row.value = value
    row.as_of = as_of
    row.source = source
    row.detail = detail
    db.commit()
    db.refresh(row)
    return row


def upsert_macro_indicators(db: Session, records: List[dict]) -> int:
    """批量写入市场指标读数。records 形如 [{key, value, as_of, source, detail}]。

    updated_at 显式写 CURRENT_TIMESTAMP：本表是「最新快照」，读数与上次完全相同时也
    要留下「本次抓取成功落库」的痕迹，页面据此显示「数据更新于 …」并判断定时任务是否
    还在跑。只靠 onupdate 的话，读数不变时 SQLAlchemy 不会发 UPDATE，时间会一直停在
    上一次数值变化那天——长假/报告期未变时会被误判成调度器挂了。
    """
    for rec in records:
        row = get_macro_indicator(db, rec["key"])
        if row is None:
            row = models.MacroIndicator(key=rec["key"])
            db.add(row)
        row.value = rec.get("value")
        row.as_of = rec.get("as_of")
        row.source = rec.get("source", "akshare")
        row.detail = rec.get("detail", "{}")
        row.updated_at = func.now()
    db.commit()
    return len(records)


def seed_macro_fallbacks(db: Session, defaults: Dict[str, dict]) -> int:
    """为尚未入库的指标写入兜底读数（已存在则不覆盖），返回新增条数。

    defaults 形如 {"excess_reserve": {"value": 1.3, "as_of": "兜底默认值"}}。
    这些记录 source 标记为 fallback，首次自动抓取成功后会被真实读数替换。
    """
    existing = {row[0] for row in db.query(models.MacroIndicator.key).all()}
    created = 0
    for key, meta in defaults.items():
        if key in existing:
            continue
        db.add(models.MacroIndicator(
            key=key,
            value=meta.get("value"),
            as_of=meta.get("as_of", "兜底默认值"),
            source="fallback",
            detail="{}",
        ))
        created += 1
    if created:
        db.commit()
    return created


def get_macro_last_updated(db: Session, source: Optional[str] = None):
    """返回最近一次更新的时间（用于判断缓存是否过期）。"""
    query = db.query(func.max(models.MacroIndicator.updated_at))
    if source:
        query = query.filter(models.MacroIndicator.source == source)
    return query.scalar()


# ------------------------------------------------------------ 市场指标历史

def _same_reading(row, rec: dict) -> bool:
    """历史记录与待写入读数是否等价（数据日期相同、数值相同）。"""
    if (row.as_of or "") != (rec.get("as_of") or ""):
        return False
    old, new = row.value, rec.get("value")
    if old is None and new is None:
        return True
    if old is None or new is None:
        return False
    try:
        return abs(float(old) - float(new)) < 1e-9
    except (TypeError, ValueError):
        return False


def add_macro_history(db: Session, records: List[dict]) -> int:
    """把本次抓到的读数写进历史表，返回新增条数。

    判重口径与 bulk_upsert_macro_history 完全一致：按「指标 + 数据日期」定位。
    同一 (key, as_of) 已存在且读数相同则跳过；读数被修正（同一天抓到不同值）
    则就地更新那一行，而不是再追加一条。

    ⚠️ 这里**不能用「该指标 id 最大的那条」当比较基准**：历史表里 as_of 最新的
    记录 id 未必最大（首次引入历史时 seed 的行 id 很小，后续回填插入的行 id 在
    另一段），按 id 取「最后一条」会比对错对象，进而追加出同 (key, as_of) 的
    重复点，曲线末端于是出现重叠的数据点。
    """
    if not records:
        return 0
    return bulk_upsert_macro_history(db, records)["added"]


def seed_macro_history_from_snapshot(db: Session) -> int:
    """把快照表里已有的读数补写进历史表（仅针对历史表中尚无记录的指标）。

    用于首次引入历史表时，让历史从「当前读数」开始，而不是空表。
    """
    has_history = {row[0] for row in db.query(models.MacroIndicatorHistory.key).distinct().all()}
    records = []
    for row in db.query(models.MacroIndicator).all():
        if row.key in has_history:
            continue
        records.append({
            "key": row.key,
            "value": row.value,
            "as_of": row.as_of,
            "source": row.source,
            "detail": row.detail,
        })
    return add_macro_history(db, records)


def get_macro_history(
    db: Session,
    key: Optional[str] = None,
    limit: int = 200,
    order: str = "desc",
) -> List[models.MacroIndicatorHistory]:
    """查询历史读数。key 为空时返回全部指标（按抓取时间排序，默认最新在前）。"""
    query = db.query(models.MacroIndicatorHistory)
    if key:
        query = query.filter(models.MacroIndicatorHistory.key == key)
    if order == "asc":
        query = query.order_by(models.MacroIndicatorHistory.id.asc())
    else:
        query = query.order_by(models.MacroIndicatorHistory.id.desc())
    return query.limit(limit).all()


def count_macro_history(db: Session, key: Optional[str] = None) -> int:
    """历史记录条数（可按指标过滤）。"""
    query = db.query(func.count(models.MacroIndicatorHistory.id))
    if key:
        query = query.filter(models.MacroIndicatorHistory.key == key)
    return int(query.scalar() or 0)


def bulk_upsert_macro_history(db: Session, records: List[dict]) -> Dict[str, int]:
    """按 (key, as_of) 批量写入历史（回溯补齐与快照刷新共用），返回 {"added", "updated"}。

    按「指标 + 数据日期」定位，是全项目**唯一**的历史表写入口径：
    - 该 (key, as_of) 尚不存在 → 新增一行
    - 已存在且数值相同 → 跳过
    - 已存在但数值不同 → 就地更新（视为对历史读数的修正）

    这样反复执行回溯不会堆重复行，也能修正早期用错锚（如政策利率已调整）的旧值。
    add_macro_history（快照刷新用）也委托到这里，保证两条写入路径判重一致。
    """
    if not records:
        return {"added": 0, "updated": 0}

    keys = {rec["key"] for rec in records}
    existing = {
        (row.key, row.as_of): row
        for row in db.query(models.MacroIndicatorHistory)
        .filter(models.MacroIndicatorHistory.key.in_(keys))
        .all()
    }

    added = 0
    updated = 0
    for rec in records:
        row = existing.get((rec["key"], rec.get("as_of")))
        if row is None:
            row = models.MacroIndicatorHistory(
                key=rec["key"],
                value=rec.get("value"),
                as_of=rec.get("as_of"),
                source=rec.get("source", "akshare"),
                detail=rec.get("detail", "{}"),
            )
            db.add(row)
            existing[(rec["key"], rec.get("as_of"))] = row
            added += 1
            continue
        if _same_reading(row, rec):
            continue
        row.value = rec.get("value")
        row.source = rec.get("source", row.source)
        row.detail = rec.get("detail", row.detail)
        updated += 1

    if added or updated:
        db.commit()
    return {"added": added, "updated": updated}


def get_macro_history_series(
    db: Session,
    keys: List[str],
    per_key_limit: int = 3000,
) -> Dict[str, List[models.MacroIndicatorHistory]]:
    """按指标取历史序列（时间正序），供图表使用。

    按 as_of（数据日期）排序而非自增 id：回溯补齐前已存在的「最新一期」记录
    id 很小，若按 id 排会插到序列开头，导致折线图乱序。
    每个指标最多取 per_key_limit 条最新记录，再按时间正序返回。
    """
    result: Dict[str, List[models.MacroIndicatorHistory]] = {k: [] for k in keys}
    if not keys:
        return result

    for key in keys:
        rows = (
            db.query(models.MacroIndicatorHistory)
            .filter(models.MacroIndicatorHistory.key == key)
            .order_by(
                models.MacroIndicatorHistory.as_of.desc(),
                models.MacroIndicatorHistory.id.desc(),
            )
            .limit(per_key_limit)
            .all()
        )
        rows.reverse()  # 转成时间正序
        result[key] = rows
    return result


# ================================================================ 股票指标
#
# 「股票指标」页（A 股冷热三层温度计）的快照表与历史表。写入口径与市场指标
# 完全一致（历史表按 (key, as_of) 唯一定位），只是换成独立的表，避免与宏观
# 指标的待刷新判定、缓存有效期互相干扰。下面的历史读写共用一份泛型实现。


def _bulk_upsert_history(db: Session, model, records: List[dict]) -> Dict[str, int]:
    """泛型历史写入：按 (key, as_of) 定位，返回 {"added", "updated"}。

    - 该 (key, as_of) 尚不存在 → 新增
    - 已存在且数值相同 → 跳过
    - 已存在但数值不同 → 就地修正（早期回填口径与最新抓取口径不一致时用得上）
    """
    if not records:
        return {"added": 0, "updated": 0}

    keys = {rec["key"] for rec in records}
    existing = {
        (row.key, row.as_of): row
        for row in db.query(model).filter(model.key.in_(keys)).all()
    }

    added = 0
    updated = 0
    for rec in records:
        row = existing.get((rec["key"], rec.get("as_of")))
        if row is None:
            row = model(
                key=rec["key"],
                value=rec.get("value"),
                as_of=rec.get("as_of"),
                source=rec.get("source", "akshare"),
                detail=rec.get("detail", "{}"),
            )
            db.add(row)
            existing[(rec["key"], rec.get("as_of"))] = row
            added += 1
            continue
        if _same_reading(row, rec):
            continue
        row.value = rec.get("value")
        row.source = rec.get("source", row.source)
        row.detail = rec.get("detail", row.detail)
        updated += 1

    if added or updated:
        db.commit()
    return {"added": added, "updated": updated}


def _history_series(db: Session, model, keys: List[str], per_key_limit: int):
    """泛型历史序列取数：按 as_of（数据日期）排序，返回时间正序。"""
    result: Dict[str, list] = {k: [] for k in keys}
    for key in keys:
        rows = (
            db.query(model)
            .filter(model.key == key)
            .order_by(model.as_of.desc(), model.id.desc())
            .limit(per_key_limit)
            .all()
        )
        rows.reverse()
        result[key] = rows
    return result


# ------------------------------------------------------------ 股票指标快照

def get_stock_temp_indicators(db: Session) -> Dict[str, models.StockTempIndicator]:
    """返回全部股票指标最新读数，按 key 索引。"""
    return {row.key: row for row in db.query(models.StockTempIndicator).all()}


def get_stock_temp_indicator(db: Session, key: str) -> Optional[models.StockTempIndicator]:
    return (
        db.query(models.StockTempIndicator)
        .filter(models.StockTempIndicator.key == key)
        .first()
    )


def upsert_stock_temp_indicators(db: Session, records: List[dict]) -> int:
    """批量写入股票指标读数。records 形如 [{key, value, as_of, source, detail}]。

    updated_at 显式写 CURRENT_TIMESTAMP，口径与 upsert_macro_indicators 一致：
    读数没变也要记录「本次抓取落库时间」，页面据此显示「数据更新于 …」。
    """
    for rec in records:
        row = get_stock_temp_indicator(db, rec["key"])
        if row is None:
            row = models.StockTempIndicator(key=rec["key"])
            db.add(row)
        row.value = rec.get("value")
        row.as_of = rec.get("as_of")
        row.source = rec.get("source", "akshare")
        row.detail = rec.get("detail", "{}")
        row.updated_at = func.now()
    db.commit()
    return len(records)


def get_stock_temp_last_updated(db: Session, source: Optional[str] = None):
    """最近一次更新时间（判断缓存是否过期）。"""
    query = db.query(func.max(models.StockTempIndicator.updated_at))
    if source:
        query = query.filter(models.StockTempIndicator.source == source)
    return query.scalar()


# ------------------------------------------------------------ 股票指标历史

def bulk_upsert_stock_temp_history(db: Session, records: List[dict]) -> Dict[str, int]:
    return _bulk_upsert_history(db, models.StockTempHistory, records)


def add_stock_temp_history(db: Session, records: List[dict]) -> int:
    """快照刷新时把读数写进历史表（与回填共用同一判重口径）。"""
    return bulk_upsert_stock_temp_history(db, records)["added"]


def seed_stock_temp_history_from_snapshot(db: Session) -> int:
    """把快照表已有读数补写进历史表（仅针对历史表中尚无记录的指标）。"""
    has_history = {
        row[0] for row in db.query(models.StockTempHistory.key).distinct().all()
    }
    records = []
    for row in db.query(models.StockTempIndicator).all():
        if row.key in has_history:
            continue
        records.append({
            "key": row.key,
            "value": row.value,
            "as_of": row.as_of,
            "source": row.source,
            "detail": row.detail,
        })
    return add_stock_temp_history(db, records)


def get_stock_temp_history_series(
    db: Session, keys: List[str], per_key_limit: int = 3000
) -> Dict[str, List[models.StockTempHistory]]:
    return _history_series(db, models.StockTempHistory, keys, per_key_limit)


def get_stock_temp_history(
    db: Session, key: Optional[str] = None, limit: int = 200, order: str = "desc"
) -> List[models.StockTempHistory]:
    query = db.query(models.StockTempHistory)
    if key:
        query = query.filter(models.StockTempHistory.key == key)
    if order == "asc":
        query = query.order_by(models.StockTempHistory.id.asc())
    else:
        query = query.order_by(models.StockTempHistory.id.desc())
    return query.limit(limit).all()


def count_stock_temp_history(db: Session, key: Optional[str] = None) -> int:
    query = db.query(func.count(models.StockTempHistory.id))
    if key:
        query = query.filter(models.StockTempHistory.key == key)
    return int(query.scalar() or 0)


# ------------------------------------------------------------ 今日热点快照

def save_hot_sector_snapshots(
    db: Session,
    trade_date: str,
    sectors: List[dict],
    reason_source: str = "rule",
) -> int:
    """按 (交易日, 板块代码) 更新或插入今日热点板块快照，供后续复盘。

    sectors 为 app/utils/hot_sector.py 组装好的板块列表（含 stocks 明细）。
    同一交易日同一板块重复抓取时就地覆盖：盘后数据最完整，复盘只看当日最终结果。
    """
    for sector in sectors:
        row = (
            db.query(models.HotSectorSnapshot)
            .filter(
                models.HotSectorSnapshot.trade_date == trade_date,
                models.HotSectorSnapshot.board_code == sector["code"],
            )
            .first()
        )
        if row is None:
            row = models.HotSectorSnapshot(trade_date=trade_date, board_code=sector["code"])
            db.add(row)

        row.board_rank = sector.get("rank")
        row.board_name = sector.get("name") or ""
        row.change_percent = sector.get("change_percent")
        row.turnover_rate = sector.get("turnover_rate")
        row.up_count = sector.get("up_count")
        row.down_count = sector.get("down_count")
        row.lead_stock = sector.get("lead_stock")
        row.lead_stock_change = sector.get("lead_stock_change")
        row.reason = sector.get("reason")
        row.reason_source = reason_source
        row.stocks = json.dumps(sector.get("stocks") or [], ensure_ascii=False)
        row.fetched_at = func.now()
        row.updated_at = func.now()

    db.commit()
    return len(sectors)


def get_latest_hot_sector_snapshot(db: Session):
    """读取库中最新一个交易日的全部热点板块快照（供「今日热点」页只读展示）。

    返回 (trade_date, rows, fetched_at)：
    - trade_date：库中最大的交易日（无数据时为 None）
    - rows      ：该交易日全部板块行，按板块排名正序
    - fetched_at：该交易日最近一次抓取时间（UTC，与库表 func.now() 同基准）
    库中无数据时返回 (None, [], None)。
    """
    latest_date = db.query(func.max(models.HotSectorSnapshot.trade_date)).scalar()
    if not latest_date:
        return None, [], None

    rows = (
        db.query(models.HotSectorSnapshot)
        .filter(models.HotSectorSnapshot.trade_date == latest_date)
        .order_by(models.HotSectorSnapshot.board_rank)
        .all()
    )
    fetched_at = max((r.fetched_at for r in rows if r.fetched_at), default=None)
    return latest_date, rows, fetched_at
