"""股票指标（A 股冷热三层温度计）数据抓取与落库（供 API 与定时任务共用）。

与宏观指标模块（app/utils/macro_refresh.py）同构，把「抓取 → 落库」从接口层抽出来，
让 Web 接口与后台定时任务用同一份实现，避免两处逻辑漂移：

- refresh_snapshot() ：抓取全部指标的当前读数，写 stock_temp_indicators（最新快照）
                       与 stock_temp_history（历史，读数变化才追加）
- backfill_history() ：各数据源自带历史的指标直接整段回填；交易所日频数据（成交额 /
                       换手率 / 流通市值）按最近 N 个交易日逐日补齐，幂等可重复执行。
                       **只由「股票趋势」页的「补齐历史数据」按钮触发，不在定时任务里跑**

定时抓取由外部自动任务在北京时间 16:00 调用 POST /api/crawl/daily-refresh 点火
（见 app/api/crawl.py）；定时任务只调用 refresh_snapshot() 抓当前读数，不做历史回溯。
"""
import logging
from datetime import date
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import greed_index as gi
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


def _fill_greed_percentiles(db: Session, records: list) -> int:
    """按市场贪婪指数口径给 7 个分项补算**实测分位**并回写快照。

    与 _fill_percentiles（综合温度计口径）的区别（两者刻意不同，不要合并）：
    - 窗口：项目历史表逐日累计，有多少期用多少期（上限 greed_index.GREED_WINDOW）
    - 分子：严格小于当前值的样本数，非小于等于
    - 范围：贪婪指数需要的 7 个 key（宽度 / 换手率 / 成交额 / 融资余额占比 /
      沪深300 PE-TTM / ERP / 破净率），且不要求原指标的 judge 是 percentile

    ⚠️ **两种样本门槛，别一刀切**（2026-10-20 实测踩过）：
      · 主成分（有基准分位兜底）：必须攒够 `GREED_BASE_MIN_SAMPLES`(20) 期才用实测。
        实测 2~3 期时融资占比与 PE 的分位都算出 0%，把贪婪指数从 37.4 压到 25.7 ——
        样本极少时「严格小于当前值」的个数只能是 0 或 1，分位必然贴 0%/100%，是伪信号。
      · 确认层 ERP / 破净率（无基准值兜底）：样本够 `GREED_MIN_SAMPLES`(2) 就得给，
        否则泡沫警示闸门永远无法触发。

    ⚠️ **分位只来自项目历史表**（用户 2026-10-20 定案）：
    样本满 greed_index.GREED_BASE_MIN_SAMPLES 期后用实测分位覆盖基准值，
    不够则留 None —— 由 build_greed_index 回落到 V3.6c 基准分位兜底。
    **刻意不用** fetch 自带序列与长历史源：
      · fetch 自带序列（legulegu/东财）与基准分位的口径不同
      · 长历史源是「指数成交量×均价」近似口径，实测会算出与基准矛盾的95%
    所以这里没有任何序列来源，只有历史表逐日积累这一条路。

    ⚠️ **margin 特殊：历史表存的是「占流通市值%」**，与value 同量纲，
    可以直接算分位（早期版本曾用两融余额分位与之混算，量纲错配，已废弃）。

    结果写进 detail 的「贪婪分位 / 贪婪分位窗口 / 贪婪分位样本数」三个**独立键**，
    不动原有的「分位」——综合温度计照旧读「分位」，贪婪指数读「贪婪分位」，
    两套窗口各自滚动、互不干扰。
    """
    need = []
    for rec in records:
        key = rec.get("key")
        if key not in gi.GREED_KEYS:
            continue
        ind = st.INDICATOR_MAP.get(key)
        if not ind:
            continue
        if st.load_detail(rec.get("detail")).get("贪婪分位") is not None:
            continue
        need.append(key)
    if not need:
        return 0

    grouped = crud.get_stock_temp_history_series(db, need, per_key_limit=gi.GREED_WINDOW)
    fixed = []
    for rec in records:
        key = rec.get("key")
        if key not in need:
            continue
        cur = rec.get("value")
        as_of = rec.get("as_of")

        # 项目历史表逐日读数 + 当期快照值（同量纲，直接可比）
        rows = grouped.get(key) or []
        series = [{"as_of": r.as_of, "value": float(r.value)}
                  for r in rows if r.value is not None and r.as_of]
        if cur is not None and as_of:
            series = [p for p in series if p["as_of"] != as_of]
            series.append({"as_of": as_of, "value": float(cur)})
            series.sort(key=lambda p: p["as_of"])

        n = gi.percentile_sample_size(series)
        # 样本门槛按角色区分（关键，别一刀切）：
        # · **主成分**有基准分位兜底 → 必须攒够 GREED_BASE_MIN_SAMPLES 期才用实测，
        #   否则用 2~3 期算出的分位必然贴 0%/100%（实测把指数从 37.4 压到 25.7）。
        # · **确认层**（ERP / 破净率）没有基准值可用 → 样本够GREED_MIN_SAMPLES
        #   就得给，否则泡沫警示闸门永远无法触发。
        need_samples = (gi.GREED_BASE_MIN_SAMPLES
                        if key in gi.GREED_BASE_PERCENTILES
                        else gi.GREED_MIN_SAMPLES)

        pct, window = (None, None)
        if n >= need_samples:
            pct, window = gi.greed_percentile(cur, series)
            if pct is None:
                window = "样本 %d 期但分位不可算" % n
        else:
            # 主成分样本不够 → pct 留 None，由 build_greed_index 回落到基准分位
            window = "已累计 %d / %d 期" % (n, need_samples)

        detail = st.load_detail(rec.get("detail"))
        detail["贪婪分位"] = pct
        detail["贪婪分位窗口"] = window
        if pct is not None:
            detail["贪婪分位来源"] = "项目历史表累计 %d 期" % n
        # 样本期数**无论有没有分位都记**：这是「什么时候会切到实测分位」的唯一
        # 可见依据，也是基准分位阶段的「再攒 N 期」提示的数据来源。
        detail["贪婪分位样本数"] = n
        rec["detail"] = st.dump_detail(detail)
        fixed.append(rec)

    if fixed:
        # 不在这里 upsert 快照：调用方refresh_snapshot 会在补算后统一落库，
        # 避免同一批数据写两次快照（云库每次 upsert 都是一次网络往返）。
        logger.info("贪婪指数分位补算: %s", [
            "%s=%s(%s期)" % (
                r["key"], st.load_detail(r["detail"]).get("贪婪分位"),
                st.load_detail(r["detail"]).get("贪婪分位样本数"),
            ) for r in fixed
        ])
    return len(fixed)


def _record_greed(db: Session) -> int:
    """把当前市场贪婪指数打卡写进历史表（key="greed_index"），供 5 日移动平均与后续曲线用。

    取最近 4 个历史点 + 当日值算 5 日移动平均；样本不足时 ma5 为 None，
    落库写当日原始值（平滑是可选展示项，不可用时不能挡掉打卡）。
    """
    raw = _snapshot_records(db)
    payload = st.build_response(raw)
    greed = payload.get("greed_index") or {}
    value = greed.get("value")
    if value is None:
        return 0

    prev = crud.get_stock_temp_history(db, key="greed_index", limit=gi.SMOOTH_DAYS - 1, order="desc")
    history = [r.value for r in prev if r.value is not None]
    ma5 = gi.moving_average(list(reversed(history)) + [value], gi.SMOOTH_DAYS)

    detail = {
        "贪婪指数": value,
        "MA5": ma5,
        "情绪层": next((f["score"] for f in greed.get("factors") or []
                       if f["layer"] == "sentiment" and f["score"] is not None), None),
        "估值层": next((f["score"] for f in greed.get("factors") or []
                       if f["layer"] == "valuation" and f["score"] is not None), None),
        "ERP分位": next((c["percentile"] for c in greed.get("confirms") or []
                         if c["key"] == "erp"), None),
        "破净率分位": next((c["percentile"] for c in greed.get("confirms") or []
                          if c["key"] == "below_na"), None),
        "状态": greed.get("state"),
        "信号": greed.get("signal"),
        "覆盖率(%)": greed.get("coverage"),
        "深度确认": (greed.get("deep") or {}).get("title"),
        "数据源": "由 5 个分项历史分位加权合成（情绪 88% + 估值 12%）",
    }
    today = date.today().strftime("%Y-%m-%d")
    return crud.add_stock_temp_history(db, [{
        "key": "greed_index",
        "value": value,
        "as_of": today,
        "source": "akshare",
        "detail": st.dump_detail(detail),
    }])


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
            rec = {
                "key": key,
                "value": item.get("value"),
                "as_of": item.get("as_of"),
                "source": item.get("source") or "akshare",
                "detail": st.dump_detail(item.get("detail") or {}),
            }
            records.append(rec)

        added = 0
        if records:
            # 顺序：**先补算分位，再 upsert 快照、最后落历史**。
            # 两套补算都改写 rec["detail"]，所以 upsert 必须排在补算之后 ——
            # 否则补算结果只留在内存里，快照表拿不到「贪婪分位」，
            # 页面会一直显示「基准分位」而看不到实测分位。
            #（_fill_percentiles 自身也调 upsert，是它原有的行为，这里不动。）
            _fill_percentiles(db, records)
            _fill_greed_percentiles(db, records)
            crud.upsert_stock_temp_indicators(db, records)
            added = crud.add_stock_temp_history(db, records)

        added += _record_temperature(db)
        added += _record_greed(db)

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

    各数据源自带历史的指标（PE / PB / ERP / 破净率 / 两融 / 新基金）一次整段取回；
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
