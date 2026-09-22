"""股票指标（A 股冷热三层温度计）数据抓取与落库（供 API 与定时任务共用）。

与宏观指标模块（app/utils/macro_refresh.py）同构，把「抓取 → 落库」从接口层抽出来，
让 Web 接口与后台定时任务用同一份实现，避免两处逻辑漂移：

- refresh_snapshot() ：抓取全部指标的当前读数，写 stock_temp_indicators（最新快照）
                       与 stock_temp_history（历史，读数变化才追加）
- backfill_history() ：各数据源自带历史的指标直接整段回填；交易所日频数据（成交额 /
                       换手率 / 流通市值）按最近 N 个交易日逐日补齐，幂等可重复执行。
                       **只由「股票趋势」页的「补齐历史数据」按钮触发，不在定时任务里跑**

定时任务在 app/scheduler.py 注册，由独立进程 run_scheduler.py 执行
（Web 进程默认不跑调度器，见 settings.START_SCHEDULER）；定时任务只调用
refresh_snapshot() 抓当前读数，不做历史回溯。
"""
import logging
from datetime import date
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import stock_temp as st

logger = logging.getLogger(__name__)

# 历史表条数低于此值时，回填自动升级为全量窗口（新部署 / 历史被清空后自愈）
SPARSE_HISTORY_ROWS = 300

# 直接调用 backfill_history() 且不传窗口时的默认值（只补最近几天，请求量小）。
# 页面按钮走 POST /api/stock-temp/backfill，会显式传窗口（默认 60 个交易日 / 24 个月）。
INCREMENTAL_WINDOWS = {"exchange_days": 5, "industry_months": 2}

# 手动「补齐历史」的默认窗口
BACKFILL_WINDOWS = {"exchange_days": 60, "industry_months": 24}

# 全量窗口（历史偏少时自动升级）
FULL_WINDOWS = {"exchange_days": 90, "industry_months": 36}


def _snapshot_records(db: Session) -> Dict[str, dict]:
    """库中已有读数，形如 {key: {"value","as_of","source","detail"}}（detail 为 dict）。"""
    out: Dict[str, dict] = {}
    for key, row in crud.get_stock_temp_indicators(db).items():
        out[key] = {
            "value": row.value,
            "as_of": row.as_of,
            "source": row.source,
            "detail": st.load_detail(row.detail),
        }
    return out


def _record_temperature(db: Session) -> int:
    """把当前综合温度打卡写进历史表（key="temperature"），供「温度轨迹」长期留存。"""
    raw = _snapshot_records(db)
    payload = st.build_response(raw)
    thermo = payload.get("thermometer") or {}
    if thermo.get("value") is None:
        return 0
    detail = {}
    for layer in thermo.get("layers") or []:
        if layer.get("score") is not None:
            detail[layer["name"]] = layer["score"]
    detail["三层分离度(pp)"] = thermo.get("spread")
    detail["数据源"] = "由各指标实时读数与其分位加权合成"
    today = date.today().strftime("%Y-%m-%d")
    return crud.add_stock_temp_history(db, [{
        "key": "temperature",
        "value": thermo["value"],
        "as_of": today,
        "source": "akshare",
        "detail": st.dump_detail(detail),
    }])


def _freq_code(ind: dict) -> str:
    freq = str(ind.get("freq") or "")
    if freq.startswith("日"):
        return "D"
    if freq.startswith("周"):
        return "W"
    return "M"


def _fill_percentiles(db: Session, records: list) -> int:
    """给「按分位判定」但本次抓取没算出分位的指标，用历史表补算分位并回写快照。

    交易所日频指标（成交额 / 换手率）和产业资本的一次抓取窗口很短，算不出十年分位；
    但回填后的历史表里已经有长序列，用同一套滚动口径补算即可，避免卡片上长期挂着「—」。
    """
    need = []
    for rec in records:
        ind = st.INDICATOR_MAP.get(rec["key"])
        if not ind or ind.get("judge") != "percentile":
            continue
        if st.load_detail(rec.get("detail")).get("分位") is not None:
            continue
        need.append(rec["key"])
    if not need:
        return 0

    grouped = crud.get_stock_temp_history_series(db, need, per_key_limit=6000)
    fixed = []
    for rec in records:
        if rec["key"] not in need:
            continue
        ind = st.INDICATOR_MAP[rec["key"]]
        rows = grouped.get(rec["key"]) or []
        series = [
            {"as_of": r.as_of, "value": r.value}
            for r in rows if r.value is not None and r.as_of
        ]
        pct, window = st.percentile_rank(rec.get("value"), series, _freq_code(ind))
        if pct is None:
            continue
        detail = st.load_detail(rec.get("detail"))
        detail["分位"] = pct
        detail["分位窗口"] = window
        detail["分位来源"] = "由本页历史序列补算"
        rec["detail"] = st.dump_detail(detail)
        fixed.append(rec)

    if fixed:
        crud.upsert_stock_temp_indicators(db, fixed)
        logger.info("股票指标分位补算: %s", [r["key"] for r in fixed])
    return len(fixed)


def refresh_snapshot(db: Optional[Session] = None) -> Tuple[int, Dict[str, str], int]:
    """抓取全部股票指标并落库。

    返回 (成功写入快照条数, {失败指标: 原因}, 新增历史记录条数)。

    抓取失败的指标保留上一次成功读数，不用空值覆盖。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        prev = _snapshot_records(db)
        fetched = st.fetch_all(exchange_days=0, industry_months=1, prev=prev)

        records = []
        errors: Dict[str, str] = {}
        for key, item in fetched.items():
            if item.get("error"):
                errors[key] = item["error"]
                continue
            records.append({
                "key": key,
                "value": item.get("value"),
                "as_of": item.get("as_of"),
                "source": item.get("source") or "akshare",
                "detail": st.dump_detail(item.get("detail") or {}),
            })

        added = 0
        if records:
            crud.upsert_stock_temp_indicators(db, records)
            added = crud.add_stock_temp_history(db, records)
            # 单次抓取窗口太短、算不出分位的指标，用历史表补算后再打卡温度
            if _fill_percentiles(db, records):
                crud.add_stock_temp_history(db, records)

        added += _record_temperature(db)

        logger.info(
            "股票指标刷新: 快照更新 %d 项, 历史新增 %d 条, 失败 %d 项",
            len(records), added, len(errors),
        )
        return len(records), errors, added
    finally:
        if own:
            db.close()


def backfill_history(
    db: Optional[Session] = None,
    exchange_days: Optional[int] = None,
    industry_months: Optional[int] = None,
    escalate_if_sparse: bool = True,
) -> Dict[str, Any]:
    """回溯历史序列并写入历史表（幂等：按「指标 + 数据日期」覆盖写）。

    各数据源自带历史的指标（PE / PB / ERP / 破净率 / 两融 / 公募仓位 / 新基金）一次整段取回；
    交易所日频数据（成交额 / 换手率 / 流通市值）需要逐日请求，按 exchange_days 限制窗口；
    产业资本按「滚动 30 日」逐月回填 industry_months 个月。

    历史表条数少于 SPARSE_HISTORY_ROWS 时自动升级为全量窗口。

    返回 {generated, added, updated, counts, errors, history_total, escalated, windows}。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        escalated = False
        defaults = INCREMENTAL_WINDOWS
        if escalate_if_sparse and crud.count_stock_temp_history(db) < SPARSE_HISTORY_ROWS:
            defaults = FULL_WINDOWS
            escalated = True
            logger.info("股票指标历史偏少，本次回填升级为全量窗口 (%s)", FULL_WINDOWS)

        exchange_days = defaults["exchange_days"] if exchange_days is None else max(0, exchange_days)
        industry_months = defaults["industry_months"] if industry_months is None else max(1, industry_months)

        prev = _snapshot_records(db)
        result = st.fetch_all(
            exchange_days=exchange_days, industry_months=industry_months, prev=prev
        )

        records = []
        counts: Dict[str, int] = {}
        errors: Dict[str, str] = {}
        for key, item in result.items():
            if item.get("error"):
                errors[key] = item["error"]
                continue
            series = item.get("series")
            if series:
                for p in series:
                    if p.get("value") is None or not p.get("as_of"):
                        continue
                    records.append({
                        "key": key,
                        "value": p["value"],
                        "as_of": p["as_of"],
                        "source": item.get("source") or "akshare",
                        "detail": "{}",
                    })
                counts[key] = len(series)
            else:
                # 无自带历史的指标（宽度 / ETF 份额 / 结构分化）：至少落下当前读数
                if item.get("value") is not None and item.get("as_of"):
                    records.append({
                        "key": key,
                        "value": item["value"],
                        "as_of": item["as_of"],
                        "source": item.get("source") or "akshare",
                        "detail": st.dump_detail(item.get("detail") or {}),
                    })
                    counts[key] = 1
                    continue
                counts[key] = 0

        written = crud.bulk_upsert_stock_temp_history(db, records)
        logger.info(
            "股票指标回填: 生成 %d 条, 新增 %d, 更新 %d, 失败 %s",
            len(records), written["added"], written["updated"], errors,
        )
        return {
            "generated": len(records),
            "added": written["added"],
            "updated": written["updated"],
            "counts": counts,
            "errors": errors,
            "history_total": crud.count_stock_temp_history(db),
            "escalated": escalated,
            "windows": {"exchange_days": exchange_days, "industry_months": industry_months},
        }
    finally:
        if own:
            db.close()
