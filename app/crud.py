import json
import logging
from typing import Any, List, Optional, Dict, Set
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import case, desc, func, or_

from app import models, schemas
from app import cloud_db as _cdb

logger = logging.getLogger(__name__)

# ================================================================ 云库分支开关
#
# 快照类小表（macro_indicators / stock_temp_indicators / 各自历史表 / crawl_logs）
# 已迁移到WorkBuddy 云数据库（PostgREST）。启用条件是三个环境变量齐备：
#   CLOUD_DB_ENABLED=true / CLOUD_DB_ENDPOINT / CLOUD_DB_KEY
# 未启用时**原样走 SQLite**，本地开发与离线调试不受任何影响。
#
# 两个后端都返回带属性的行对象（ORM 实例 / cloud_db.Row），
# 上层调用方（stock_temp.py / macro_* 等）无需区分，代码一行都不用改。

_USE_CLOUD = _cdb.cloud_db_configured()

# 表名常量（云库侧用字符串表名，SQLite 侧用 ORM 类）
T_MACRO_SNAP = "macro_indicators"
T_MACRO_HIST = "macro_indicator_history"
T_TEMP_SNAP = "stock_temp_indicators"
T_TEMP_HIST = "stock_temp_history"
T_CRAWL_LOG = "crawl_logs"

# ---- 大表（第二步迁移）----
T_NEWS = "news"
T_INDEX_HIST = "index_history"
T_FINANCIAL = "financial_reports"
T_MARKET_PRICE = "market_prices"
T_HOT_SECTOR = "hot_sector_snapshots"
T_STRATEGY = "market_strategy_state"
T_VALUATION = "company_valuations"

# ---------------------------------------------------------------- 新闻来源优先级
#
# 2026-10-23 用户要求：**新闻列表优先选择东方财富的新闻**。
# 排序口径 = 来源优先级 → 同来源内按发布时间倒序（两个后端一致）：
#   东方财富(0) → 同花顺(1) → 新浪财经(2) → 其他未知来源(3)
# 只影响列表展示顺序，**不改抓取逻辑、不改入库数据、不改分页总量**；
# 来源筛选下拉（source=xxx）命中单个来源时，本优先级自然退化为纯时间倒序。
NEWS_SOURCE_PRIORITY = ("东方财富", "同花顺", "新浪财经")
_NEWS_SOURCE_RANK = {name: i for i, name in enumerate(NEWS_SOURCE_PRIORITY)}


def _news_source_rank(source: Optional[str]) -> int:
    """来源优先级序号：越小越靠前，未知来源排最后。"""
    return _NEWS_SOURCE_RANK.get((source or "").strip(), len(NEWS_SOURCE_PRIORITY))


def using_cloud_db() -> bool:
    """当前是否走云数据库（页面「数据源」提示与排查用）。"""
    return _USE_CLOUD


def _like(value: str) -> str:
    """PostgREST 的 like 过滤表达式。* 在该语法里是通配符。"""
    return f"like.*{value}*"


def _esc_or(value: str) -> str:
    """转义 OR 表达式里的特殊字符（含逗号/括号会让 PostgREST 语法解析失败）。"""
    return value.replace(",", " ").replace("(", " ").replace(")", " ")


def _as_utc(value) -> Optional[datetime]:
    """把行里的时间列统一成 **naive UTC**，用于与 datetime.now() 比较。

    SQLite 侧的 publish_time 由抓取代码写入，多为**本地时间**；
    云库侧是 TIMESTAMP 列。这里统一按「naive 就是本地时间」处理，
    与SQLite 分支的过滤口径保持一致（否则同一批数据两套时区，页面结果会不一样）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    from datetime import timezone as _tz

    text = str(value).strip().replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return parsed.astimezone(_tz.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _text_has(haystack, needle: str) -> bool:
    """标题/正文包含判断（对齐 SQLAlchemy 的 .contains()，即子串匹配）。"""
    if haystack is None:
        return False
    return needle in str(haystack)


def get_news(db: Session, news_id: int) -> Optional[models.News]:
    if _USE_CLOUD:
        return _cdb.select_one(T_NEWS, filters={"id": f"eq.{news_id}"})
    return db.query(models.News).filter(models.News.id == news_id).first()


def get_news_by_url(db: Session, url: str) -> Optional[models.News]:
    if _USE_CLOUD:
        return _cdb.select_one(T_NEWS, filters={"url": f"eq.{url}"})
    return db.query(models.News).filter(models.News.url == url).first()


def is_news_exists(db: Session, url: str) -> bool:
    return get_news_by_url(db, url) is not None


def get_news_list(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    source: Optional[str] = None,
    include_keywords: Optional[List[str]] = None,
    exclude_keywords: Optional[List[str]] = None,
    today_only: bool = False,
    all_history: bool = False
) -> List[models.News]:
    # ---- 云库分支：PostgREST 无法表达「多个 AND 各带一个 OR 组」的任意组合，
    # 因此关键词过滤改成逐关键词查询后在内存里求交集 —— 关键词数量很少（通常 1~3个），
    # 代价是几次往返，收益是语义与 SQLite 分支完全一致。
    if _USE_CLOUD:
        if all_history:
            rows = _cdb.select_all(T_NEWS, order="publish_time.desc", max_rows=20000,
                                   context="news:all_history")
        else:
            if today_only:
                since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                since = datetime.now() - timedelta(hours=24)
            rows = _cdb.select_all(
                T_NEWS, order="publish_time.desc", max_rows=20000,
                context="news:recent",
            )
            # 截断到秒再比：云库 TIMESTAMP 列只存到秒，写入时带的微秒会被丢弃，
            # 若拿带微秒的 cutoff 去比，恰好落在边界上的那条会被误判为「超期」而丢掉。
            since = since.replace(microsecond=0)
            rows = [r for r in rows
                    if (_as_utc(r["publish_time"]) or since) >= since]

        if source:
            rows = [r for r in rows if r["source"] == source]

        for kw in (include_keywords or []):
            kw = (kw or "").strip()
            if kw:
                rows = [r for r in rows if _text_has(r["title"], kw) or _text_has(r["content"], kw)]
        for kw in (exclude_keywords or []):
            kw = (kw or "").strip()
            if kw:
                rows = [r for r in rows
                        if not (_text_has(r["title"], kw) or _text_has(r["content"], kw))]

        # 展示排序：先按发布时间倒序，再按来源优先级做**稳定**排序
        # （东方财富整组提到最前，组内保持时间倒序；其余来源同理）
        rows.sort(key=lambda r: str(r["publish_time"] or ""), reverse=True)
        rows.sort(key=lambda r: _news_source_rank(r["source"]))

        return rows[skip:skip + limit] if (skip or limit) else rows

    query = db.query(models.News)

    if all_history:
        # 查询全部历史（含导入的历史库数据），不做时间过滤。
        # 仅供显式请求历史时使用，首页默认仍走当日 / 最近 24 小时。
        pass
    elif today_only:
        # 首页默认只展示当日新闻：按自然日（服务器本地时间 00:00 起）过滤。
        # 用范围查询而非 func.date(publish_time)，避免对列套函数导致无法走索引。
        start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.filter(models.News.publish_time >= start_of_day)
    else:
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
    
    # 展示排序：来源优先级（东方财富最前）→ 同来源内发布时间倒序。
    # 用 CASE 表达式而非内存排序，保证 offset/limit 分页结果稳定。
    source_rank = case(
        *[(models.News.source == name, rank) for rank, name in enumerate(NEWS_SOURCE_PRIORITY)],
        else_=len(NEWS_SOURCE_PRIORITY),
    )
    query = query.order_by(source_rank, desc(models.News.publish_time))
    
    return query.offset(skip).limit(limit).all()


def create_news(db: Session, news: schemas.NewsCreate) -> models.News:
    data = news.model_dump()
    if _USE_CLOUD:
        # publish_time 是本地时间语义，写库前转成 ISO 字符串（云库列是 TIMESTAMP）
        pt = data.get("publish_time")
        if isinstance(pt, datetime):
            data["publish_time"] = pt.isoformat(sep=" ", timespec="seconds")
        rows = _cdb.upsert(T_NEWS, [data], on_conflict="url", context="create_news")
        return rows[0] if rows else _cdb.Row(data)
    db_news = models.News(**data)
    db.add(db_news)
    db.commit()
    db.refresh(db_news)
    return db_news


def update_news(db: Session, news_id: int, news: schemas.NewsUpdate) -> Optional[models.News]:
    if _USE_CLOUD:
        payload = news.model_dump(exclude_unset=True)
        if not payload:
            return get_news(db, news_id)
        pt = payload.get("publish_time")
        if isinstance(pt, datetime):
            payload["publish_time"] = pt.isoformat(sep=" ", timespec="seconds")
        resp = _cdb._safe("PATCH", f"/{T_NEWS}", default=None,
                          params={"id": f"eq.{news_id}"}, json_body=payload,
                          prefer="return=representation", context="update_news")
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        items = data if isinstance(data, list) else [data]
        return _cdb.Row(items[0]) if items else None

    db_news = get_news(db, news_id)
    if db_news:
        update_data = news.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_news, key, value)
        db.commit()
        db.refresh(db_news)
    return db_news


def delete_news(db: Session, news_id: int) -> bool:
    if _USE_CLOUD:
        return _cdb.delete(T_NEWS, filters={"id": f"eq.{news_id}"},
                           context="delete_news") > 0
    db_news = get_news(db, news_id)
    if db_news:
        db.delete(db_news)
        db.commit()
        return True
    return False


def get_all_market_prices(db: Session) -> List[models.MarketPrice]:
    if _USE_CLOUD:
        return _cdb.select_all(T_MARKET_PRICE, order="symbol.asc,date.asc",
                               max_rows=20000, context="get_all_market_prices")
    return db.query(models.MarketPrice).all()


def upsert_market_price(db: Session, price: schemas.MarketPriceCreate) -> models.MarketPrice:
    """按 (symbol, date) 更新或插入一条历史记录。"""
    data = price.model_dump()
    if _USE_CLOUD:
        rows = _cdb.upsert(T_MARKET_PRICE, [data], on_conflict="symbol,date",
                           context="upsert_market_price")
        return rows[0] if rows else _cdb.Row(data)

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
        db_price = models.MarketPrice(**data)
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

    if _USE_CLOUD:
        rows = _cdb.select_all(
            T_MARKET_PRICE, columns="symbol,date,value",
            filters={"symbol": "in.(" + ",".join(symbols) + ")"},
            order="date.desc", max_rows=20000, context="cleanup_sparse_market_dates",
        )
        if not rows:
            return 0
        # 最近 limit 个日期
        recent_dates = []
        for r in rows:
            d = r["date"]
            if d not in recent_dates:
                recent_dates.append(d)
            if len(recent_dates) >= limit:
                break
        # 统计每个日期的非空指标数
        per_date: Dict[str, set] = {}
        for r in rows:
            if r["date"] in recent_dates and r["value"] is not None:
                per_date.setdefault(r["date"], set()).add(r["symbol"])
        sparse = [d for d in recent_dates if len(per_date.get(d, set())) < 3]
        if not sparse:
            return 0
        deleted = 0
        for d in sparse:
            deleted += _cdb.delete(
                T_MARKET_PRICE,
                filters={"symbol": "in.(" + ",".join(symbols) + ")", "date": f"eq.{d}"},
                context="cleanup_sparse_delete",
            )
        if deleted:
            logger.info(f"清理稀疏日期数据: 删除 {deleted} 行 (日期: {sparse}, 非空指标数<3)")
        return deleted

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
    if _USE_CLOUD:
        rows = _cdb.select_all(T_MARKET_PRICE, order="date.desc",
                               max_rows=20000, context="get_latest_market_prices")
        seen: Dict[str, int] = {}
        result: Dict[str, List[models.MarketPrice]] = {}
        for r in rows:                      # 已按日期倒序，每个 symbol 取前 2 条
            sym = r["symbol"]
            if seen.get(sym, 0) >= 2:
                continue
            result.setdefault(sym, []).append(r)
            seen[sym] = seen.get(sym, 0) + 1
        return result

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
    if _USE_CLOUD:
        return _cdb.select_all(T_MARKET_PRICE, filters={"symbol": f"eq.{symbol}"},
                               order="date.asc", max_rows=20000,
                               context="get_market_history")
    return (
        db.query(models.MarketPrice)
        .filter(models.MarketPrice.symbol == symbol)
        .order_by(models.MarketPrice.date.asc())
        .all()
    )


def get_market_peak(db: Session, symbol: str) -> Optional[models.MarketPrice]:
    """返回某 symbol 历史最大值记录（作为峰值，用于回撤预警）。"""
    if _USE_CLOUD:
        return _cdb.select_one(T_MARKET_PRICE, filters={"symbol": f"eq.{symbol}"},
                               order="value.desc", context="get_market_peak")
    return (
        db.query(models.MarketPrice)
        .filter(models.MarketPrice.symbol == symbol)
        .order_by(models.MarketPrice.value.desc())
        .first()
    )


def get_market_strategy_state(db: Session, symbol: str) -> Optional[models.MarketStrategyState]:
    """读取某 symbol 的峰值回撤策略状态（当前峰值/峰值日期/最近回撤触发日）。"""
    if _USE_CLOUD:
        return _cdb.select_one(T_STRATEGY, filters={"symbol": f"eq.{symbol}"})
    return (
        db.query(models.MarketStrategyState)
        .filter(models.MarketStrategyState.symbol == symbol)
        .first()
    )


def save_market_strategy_state(
    db: Session, symbol: str, peak_value: float, peak_date: str, drawdown_date: Optional[str] = None
) -> models.MarketStrategyState:
    """写入（新增或更新）某 symbol 的峰值回撤策略状态。"""
    if _USE_CLOUD:
        payload = {"symbol": symbol, "peak_value": peak_value,
                   "peak_date": peak_date, "drawdown_date": drawdown_date}
        rows = _cdb.upsert(T_STRATEGY, [payload], on_conflict="symbol",
                           context="save_market_strategy_state")
        return rows[0] if rows else _cdb.Row(payload)

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
    if _USE_CLOUD:
        return _cdb.select_all(T_INDEX_HIST, order="date.asc", max_rows=20000,
                               context="get_index_history_all")
    return (
        db.query(models.IndexHistory)
        .order_by(models.IndexHistory.date.asc())
        .all()
    )


def get_index_history_count(db: Session) -> int:
    if _USE_CLOUD:
        return _cdb.count(T_INDEX_HIST, context="get_index_history_count")
    return db.query(models.IndexHistory).count()


def delete_all_index_history(db: Session) -> int:
    """清空指数预警历史表（CSV 更新后强制重导时使用）。"""
    if _USE_CLOUD:
        n = _cdb.count(T_INDEX_HIST, context="delete_all_index_history:count")
        # 该表没有「全表删」的安全兜底（delete 拒绝无条件调用），用兜底键显式表达意图
        _cdb.delete(T_INDEX_HIST, filters={"date": "not.is.null"}, context="delete_all_index_history")
        return n
    deleted = db.query(models.IndexHistory).delete()
    db.commit()
    return deleted


def upsert_index_history_rows(db: Session, rows: List[dict]) -> int:
    """按 date 更新或插入多条指数预警历史记录。rows 形如 [{date, column, value}]。"""
    if _USE_CLOUD:
        # 先按 date 聚合（同一天可能多次赋不同列），再 upsert
        merged: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            merged.setdefault(row["date"], {"date": row["date"]})[row["column"]] = row["value"]
        payload = list(merged.values())
        _cdb.upsert(T_INDEX_HIST, payload, on_conflict="date", context="upsert_index_history")
        return len(merged)

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
    if _USE_CLOUD:
        payload = []
        for rec in records:
            item = {"stock_code": stock_code, "report_date": rec["报告期"]}
            if stock_name:
                item["stock_name"] = stock_name
            for display_name, column in FINANCIAL_FIELD_COLUMNS.items():
                item[column] = rec.get(display_name)
            payload.append(item)
        _cdb.upsert(T_FINANCIAL, payload, on_conflict="stock_code,report_date",
                    context="upsert_financial_reports")
        return len(records)

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
    if _USE_CLOUD:
        filters: Dict[str, Any] = {}
        if stock_code:
            filters["stock_code"] = f"eq.{stock_code}"
        if stock_name:
            filters["stock_name"] = f"eq.{stock_name}"
        if start:
            filters["report_date"] = f"gte.{start}"
        if end:
            # 同键的gte 与 lte 无法共存，先gte 过滤再在内存里裁上界
            rows = _cdb.select_all(T_FINANCIAL, filters=filters, order="report_date.asc",
                                   max_rows=20000, context="get_financial_reports")
            return [r for r in rows if str(r["report_date"]) <= str(end)]
        return _cdb.select_all(T_FINANCIAL, filters=filters or None, order="report_date.asc",
                               max_rows=20000, context="get_financial_reports")

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
    if _USE_CLOUD:
        rows = _cdb.select_all(T_FINANCIAL, columns="stock_name,stock_code",
                               order="stock_name.asc", max_rows=20000,
                               context="list_financial_stocks")
        seen, out = set(), []
        for r in rows:
            name = r["stock_name"]
            if not name:
                continue
            key = (name, r["stock_code"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "code": r["stock_code"]})
        return out

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
    if _USE_CLOUD:
        return _cdb.select(T_VALUATION, order="created_at.desc,id.desc", limit=limit,
                           context="get_valuation_records")
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
    if _USE_CLOUD:
        _cdb.insert(T_VALUATION, [valuation.model_dump()], context="create_valuation_record")
        # 裁剪：仅保留最近 100 条（按 id 倒序）
        keep = _cdb.select(T_VALUATION, columns="id", order="id.desc", limit=100,
                           context="valuation:trim_keep")
        keep_ids = [r["id"] for r in keep]
        if keep_ids:
            stale = _cdb.select(T_VALUATION, columns="id", order="id.desc", limit=100,
                                offset=100, context="valuation:trim_stale")
            for r in stale:
                _cdb.delete(T_VALUATION, filters={"id": f"eq.{r['id']}"},
                            context="valuation:trim_delete")
        rows = _cdb.select(T_VALUATION, order="id.desc", limit=1,
                           context="valuation:return_latest")
        return rows[0] if rows else _cdb.Row(valuation.model_dump())

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
    if _USE_CLOUD:
        return _cdb.delete(T_VALUATION, filters={"id": f"eq.{record_id}"},
                           context="delete_valuation_record") > 0
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
    if _USE_CLOUD:
        n = _cdb.count(T_VALUATION, context="clear_valuation_records:count")
        _cdb.delete(T_VALUATION, filters={"id": "not.is.null"},
                    context="clear_valuation_records")
        return n
    deleted = db.query(models.CompanyValuation).delete()
    db.commit()
    return deleted


# ---------------------------------------------------------------- 市场指标

def get_macro_indicators(db: Session) -> Dict[str, models.MacroIndicator]:
    """返回全部市场指标最新读数，按 key 索引。"""
    if _USE_CLOUD:
        return {row["key"]: row for row in _cdb.fetch_snapshot(T_MACRO_SNAP)}
    rows = db.query(models.MacroIndicator).all()
    return {row.key: row for row in rows}


def get_macro_indicator(db: Session, key: str) -> Optional[models.MacroIndicator]:
    if _USE_CLOUD:
        return _cdb.select_one(T_MACRO_SNAP, filters={"key": f"eq.{key}"})
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
    if _USE_CLOUD:
        rows = _cdb.upsert(T_MACRO_SNAP, [{
            "key": key, "value": value, "as_of": as_of,
            "source": source, "detail": detail,
        }], on_conflict="key", context="upsert_macro_indicator")
        return rows[0] if rows else _cdb.Row({"key": key, "value": value,
                                               "as_of": as_of, "source": source,
                                               "detail": detail})
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

    云库侧用 on_conflict=key 的 upsert，merge-duplicates 会连created_at 一起覆盖，
    所以 updated_at 用服务器 now() 重写，保证「本次落库时间」语义与 SQLite 侧一致。
    """
    if _USE_CLOUD:
        now = _cdb.utc_now_iso()
        payload = [{
            "key": rec["key"],
            "value": rec.get("value"),
            "as_of": rec.get("as_of"),
            "source": rec.get("source", "akshare"),
            "detail": rec.get("detail", "{}"),
            "updated_at": now,
        } for rec in records]
        return len(_cdb.upsert(T_MACRO_SNAP, payload, on_conflict="key",
                               context="upsert_macro_indicators"))
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
    existing = set(get_macro_indicators(db).keys()) if _USE_CLOUD else {
        row[0] for row in db.query(models.MacroIndicator.key).all()
    }
    created = 0
    for key, meta in defaults.items():
        if key in existing:
            continue
        if _USE_CLOUD:
            created += _cdb.insert(T_MACRO_SNAP, [{
                "key": key,
                "value": meta.get("value"),
                "as_of": meta.get("as_of", "兜底默认值"),
                "source": "fallback",
                "detail": "{}",
            }], context="seed_macro_fallbacks")
            continue
        db.add(models.MacroIndicator(
            key=key,
            value=meta.get("value"),
            as_of=meta.get("as_of", "兜底默认值"),
            source="fallback",
            detail="{}",
        ))
        created += 1
    if created and not _USE_CLOUD:
        db.commit()
    return created


def get_macro_last_updated(db: Session, source: Optional[str] = None):
    """返回最近一次更新的时间（用于判断缓存是否过期）。"""
    if _USE_CLOUD:
        return _cdb.max_updated_at(T_MACRO_SNAP, source=source)
    query = db.query(func.max(models.MacroIndicator.updated_at))
    if source:
        query = query.filter(models.MacroIndicator.source == source)
    return query.scalar()


# ------------------------------------------------------------ 市场指标历史

def _same_reading(row, rec: dict) -> bool:
    """历史记录与待写入读数是否等价（数据日期相同、数值相同）。

    兼容两种行对象：SQLite 的 ORM 实例（``row.as_of``）与云库的 ``Row``
    （``row["as_of"]``）。
    """
    def _get(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        try:
            return obj[name]
        except (TypeError, KeyError, IndexError):
            return getattr(obj, name, None)

    if (_get(row, "as_of") or "") != (rec.get("as_of") or ""):
        return False
    old, new = _get(row, "value"), rec.get("value")
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
    snapshot = get_macro_indicators(db)
    if _USE_CLOUD:
        has_history = {
            r["key"] for r in _cdb.select(T_MACRO_HIST, columns="key")
        }
        rows = list(snapshot.values())
    else:
        has_history = {
            row[0] for row in db.query(models.MacroIndicatorHistory.key).distinct().all()
        }
        rows = db.query(models.MacroIndicator).all()
    records = []
    for row in rows:
        key = row["key"] if _USE_CLOUD else row.key
        if key in has_history:
            continue
        records.append({
            "key": key,
            "value": row["value"] if _USE_CLOUD else row.value,
            "as_of": row["as_of"] if _USE_CLOUD else row.as_of,
            "source": row["source"] if _USE_CLOUD else row.source,
            "detail": row["detail"] if _USE_CLOUD else row.detail,
        })
    return add_macro_history(db, records)


def get_macro_history(
    db: Session,
    key: Optional[str] = None,
    limit: int = 200,
    order: str = "desc",
) -> List[models.MacroIndicatorHistory]:
    """查询历史读数。key 为空时返回全部指标（按抓取时间排序，默认最新在前）。

    ⚠ 云库分支不能省：原先这里只有裸 SQLAlchemy，云库模式下读的是沙箱里
    空的本地 SQLite → 恒返回空列表（与「云库模式禁止裸 SQLAlchemy」同源问题）。
    """
    if _USE_CLOUD:
        filters = {"key": f"eq.{key}"} if key else None
        desc_order = order != "asc"
        rows = _cdb.select(
            T_MACRO_HIST, filters=filters,
            order="id.desc" if desc_order else "id.asc",
            limit=limit, context="get_macro_history",
        )
        return rows

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
    if _USE_CLOUD:
        return _cdb.count(T_MACRO_HIST, filters={"key": f"eq.{key}"} if key else None)
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
    if _USE_CLOUD:
        return _cdb.upsert_history(T_MACRO_HIST, records,
                                    context="bulk_upsert_macro_history")
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
    if _USE_CLOUD:
        return _cdb.fetch_history_series(T_MACRO_HIST, keys,
                                          per_key_limit=per_key_limit)
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

    if _USE_CLOUD:
        table = T_TEMP_HIST if model is models.StockTempHistory else T_MACRO_HIST
        return _cdb.upsert_history(table, records, context="bulk_upsert_history")

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
    if _USE_CLOUD:
        table = T_TEMP_HIST if model is models.StockTempHistory else T_MACRO_HIST
        return _cdb.fetch_history_series(table, keys, per_key_limit=per_key_limit)
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
    if _USE_CLOUD:
        return {row["key"]: row for row in _cdb.fetch_snapshot(T_TEMP_SNAP)}
    return {row.key: row for row in db.query(models.StockTempIndicator).all()}


def get_stock_temp_indicator(db: Session, key: str) -> Optional[models.StockTempIndicator]:
    if _USE_CLOUD:
        return _cdb.select_one(T_TEMP_SNAP, filters={"key": f"eq.{key}"})
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
    if _USE_CLOUD:
        now = _cdb.utc_now_iso()
        payload = [{
            "key": rec["key"],
            "value": rec.get("value"),
            "as_of": rec.get("as_of"),
            "source": rec.get("source", "akshare"),
            "detail": rec.get("detail", "{}"),
            "updated_at": now,
        } for rec in records]
        return len(_cdb.upsert(T_TEMP_SNAP, payload, on_conflict="key",
                               context="upsert_stock_temp_indicators"))
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
    if _USE_CLOUD:
        return _cdb.max_updated_at(T_TEMP_SNAP, source=source)
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
    snapshot = get_stock_temp_indicators(db)
    if _USE_CLOUD:
        has_history = {
            r["key"] for r in _cdb.select(T_TEMP_HIST, columns="key",
                                          filters={"key": "not.is.null"})
        }
        rows = list(snapshot.values())
    else:
        has_history = {
            row[0] for row in db.query(models.StockTempHistory.key).distinct().all()
        }
        rows = db.query(models.StockTempIndicator).all()
    records = []
    for row in rows:
        key = row["key"] if _USE_CLOUD else row.key
        if key in has_history:
            continue
        records.append({
            "key": key,
            "value": row["value"] if _USE_CLOUD else row.value,
            "as_of": row["as_of"] if _USE_CLOUD else row.as_of,
            "source": row["source"] if _USE_CLOUD else row.source,
            "detail": row["detail"] if _USE_CLOUD else row.detail,
        })
    return add_stock_temp_history(db, records)


def get_stock_temp_history_series(
    db: Session, keys: List[str], per_key_limit: int = 3000
) -> Dict[str, List[models.StockTempHistory]]:
    return _history_series(db, models.StockTempHistory, keys, per_key_limit)


def get_stock_temp_history(
    db: Session, key: Optional[str] = None, limit: int = 200, order: str = "desc"
) -> List[models.StockTempHistory]:
    if _USE_CLOUD:
        return _cdb.select(
            T_TEMP_HIST,
            filters={"key": f"eq.{key}"} if key else None,
            order="id.asc" if order == "asc" else "id.desc",
            limit=limit, context="get_stock_temp_history",
        )
    query = db.query(models.StockTempHistory)
    if key:
        query = query.filter(models.StockTempHistory.key == key)
    if order == "asc":
        query = query.order_by(models.StockTempHistory.id.asc())
    else:
        query = query.order_by(models.StockTempHistory.id.desc())
    return query.limit(limit).all()


def count_stock_temp_history(db: Session, key: Optional[str] = None) -> int:
    if _USE_CLOUD:
        return _cdb.count(T_TEMP_HIST, filters={"key": f"eq.{key}"} if key else None)
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
    source: str = "eastmoney",
) -> int:
    """按 (交易日, 板块代码) 更新或插入今日热点板块快照，供后续复盘。

    sectors 为 app/utils/hot_sector.py 组装好的板块列表（含 stocks 明细）。
    同一交易日同一板块重复抓取时就地覆盖：盘后数据最完整，复盘只看当日最终结果。
    source 记录实际使用的数据源（eastmoney / sina），供页面标注与排查。

    ⚠ 同日残留清理：写入后删除该交易日**不在本次结果里**的旧行，覆盖两种情况：
    ① 换源 —— 东财板块代码是 BKxxxx、新浪是 gn_xxx，两者不重叠；
    ② 同源但板块换了一批 —— 今天的前三是 A/B/C，重抓变成 A/B/D，则 C 成了残留。
    需求文档的口径是「当天被收盘值替换」，故同一交易日只保留最新一次的完整结果，
    避免页面出现 6 个板块、排名重复（No.1/No.2/No.3 各出现两次）。
    """
    new_codes = {s["code"] for s in sectors}

    if _USE_CLOUD:
        now = _cdb.utc_now_iso()
        payload = [{
            "trade_date": trade_date,
            "board_code": sector["code"],
            "board_rank": sector.get("rank"),
            "board_name": sector.get("name") or "",
            "change_percent": sector.get("change_percent"),
            "main_net_inflow": sector.get("main_net_inflow"),
            "turnover_rate": sector.get("turnover_rate"),
            "up_count": sector.get("up_count"),
            "down_count": sector.get("down_count"),
            "lead_stock": sector.get("lead_stock"),
            "lead_stock_change": sector.get("lead_stock_change"),
            "reason": sector.get("reason"),
            "reason_source": reason_source,
            "source": source,
            "stocks": json.dumps(sector.get("stocks") or [], ensure_ascii=False),
            "fetched_at": now,
            "updated_at": now,
        } for sector in sectors]
        _cdb.upsert(T_HOT_SECTOR, payload, on_conflict="trade_date,board_code",
                    context="save_hot_sector_snapshots")
        _delete_stale_rows_cloud(trade_date, new_codes)
        return len(sectors)

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
        row.main_net_inflow = sector.get("main_net_inflow")
        row.turnover_rate = sector.get("turnover_rate")
        row.up_count = sector.get("up_count")
        row.down_count = sector.get("down_count")
        row.lead_stock = sector.get("lead_stock")
        row.lead_stock_change = sector.get("lead_stock_change")
        row.reason = sector.get("reason")
        row.reason_source = reason_source
        row.source = source
        row.stocks = json.dumps(sector.get("stocks") or [], ensure_ascii=False)
        row.fetched_at = func.now()
        row.updated_at = func.now()

    # 同日残留清理：删掉该交易日**不在本次结果里**的旧行。
    # 覆盖两种情况：① 换源（东财 BKxxxx vs 新浪 gn_xxx 不重叠）；
    # ② 同源但板块换了一批（今天的前三是 A/B/C，重抓变成 A/B/D → C 成了残留）。
    # 需求文档的口径是「当天被收盘值替换」，故同一交易日只保留最新一次的完整结果。
    stale = (
        db.query(models.HotSectorSnapshot)
        .filter(
            models.HotSectorSnapshot.trade_date == trade_date,
            models.HotSectorSnapshot.board_code.notin_(list(new_codes)),
        )
        .all()
    )
    for row in stale:
        db.delete(row)

    db.commit()
    return len(sectors)


def _delete_stale_rows_cloud(trade_date: str, new_codes: Set[str]) -> int:
    """云库模式：删除该交易日不在本次结果里的旧行（同日残留清理）。

    云库的 delete 只支持等值过滤，故先查出该交易日的行，再按 board_code 逐个删。
    一次最多几行，量极小。source 为 NULL 的历史行也一并清掉。
    """
    try:
        rows = _cdb.select_all(T_HOT_SECTOR, filters={"trade_date": f"eq.{trade_date}"},
                               max_rows=100, context="delete_stale_rows")
        stale = [r for r in rows if r.get("board_code") not in new_codes]
        for r in stale:
            _cdb.delete(T_HOT_SECTOR,
                        filters={"trade_date": f"eq.{trade_date}",
                                 "board_code": f"eq.{r.get('board_code')}"},
                        context="delete_stale_rows")
        if stale:
            logger.info("[hot_sector] 清理同日残留旧行 %d 条 (trade_date=%s)",
                        len(stale), trade_date)
        return len(stale)
    except Exception as e:
        logger.error("[hot_sector] 同日残留清理失败: %s", e, exc_info=True)
        return 0


def get_latest_hot_sector_snapshot(db: Session):
    """读取库中最新一个交易日的全部热点板块快照（供「今日热点」页只读展示）。

    返回 (trade_date, rows, fetched_at)：
    - trade_date：库中最大的交易日（无数据时为 None）
    - rows      ：该交易日全部板块行，按板块排名正序
    - fetched_at：该交易日最近一次抓取时间（UTC，与库表 func.now() 同基准）
    库中无数据时返回 (None, [], None)。
    """
    if _USE_CLOUD:
        rows = _cdb.select_all(T_HOT_SECTOR, order="trade_date.desc", max_rows=20000,
                               context="get_latest_hot_sector_snapshot")
        if not rows:
            return None, [], None
        latest_date = rows[0]["trade_date"]
        day_rows = [r for r in rows if r["trade_date"] == latest_date]
        day_rows.sort(key=lambda r: (r["board_rank"] is None, r["board_rank"]))
        stamps = [r["fetched_at"] for r in day_rows if r["fetched_at"] is not None]
        return latest_date, day_rows, max(stamps) if stamps else None

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


# ------------------------------------------------------------ 抓取日志

def write_crawl_log(db: Session, *, task: str, session: Optional[str] = None,
                    source: Optional[str] = None, item_count: int = 0,
                    status: str = "ok", error_message: Optional[str] = None,
                    duration: int = 0, crawl_time: Optional[datetime] = None
                    ) -> None:
    """写一条抓取日志（scheduler / 手动刷新共用）。

    日志写失败不应影响抓取主流程，故内部吞掉异常。
    云库分支下db 参数不使用（保留以维持调用签名一致）。
    """
    try:
        when = crawl_time or datetime.now()
        if _USE_CLOUD:
            # crawl_time 列在 SQLite 侧是 server_default=CURRENT_TIMESTAMP（UTC）。
            # 调用方（scheduler）传的是本地时间，云库分支统一换成 UTC 再写，
            # 否则页面按 UTC 基准算时间差会得到负数。
            stamp = _cdb.utc_now_iso() if crawl_time is None else (
                crawl_time.astimezone(timezone.utc).replace(tzinfo=None)
                .isoformat(sep=" ", timespec="microseconds")
            )
            _cdb.insert(T_CRAWL_LOG, [{
                "task": task,
                "session": session,
                "source": source,
                "item_count": item_count or 0,
                "status": status,
                "error_message": error_message,
                "duration": duration or 0,
                "crawl_time": stamp,
            }], context="write_crawl_log")
            return
        db.add(models.CrawlLog(
            task=task,
            session=session,
            source=source,
            item_count=item_count or 0,
            status=status,
            error_message=error_message,
            duration=duration or 0,
            crawl_time=when,
        ))
        db.commit()
    except Exception:
        # 日志失败静默降级：抓取结果比日志重要
        return


def list_crawl_logs(db: Session, *, limit: int = 200, task: Optional[str] = None,
                    session: Optional[str] = None) -> List[Any]:
    """读最近的抓取日志（抓取控制页展示）。"""
    if _USE_CLOUD:
        filters = {}
        if task:
            filters["task"] = f"eq.{task}"
        if session:
            filters["session"] = f"eq.{session}"
        return _cdb.select(T_CRAWL_LOG, filters=filters or None,
                           order="crawl_time.desc,id.desc", limit=limit,
                           context="list_crawl_logs")
    query = db.query(models.CrawlLog)
    if task:
        query = query.filter(models.CrawlLog.task == task)
    if session:
        query = query.filter(models.CrawlLog.session == session)
    return query.order_by(models.CrawlLog.crawl_time.desc(),
                          models.CrawlLog.id.desc()).limit(limit).all()
