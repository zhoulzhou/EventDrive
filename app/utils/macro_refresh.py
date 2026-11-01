"""市场指标数据抓取与落库（供 API 与定时任务共用）。

把「抓取 → 落库」从接口层抽出来，让 Web 接口与后台定时任务用同一份实现，
避免两处逻辑漂移：

- refresh_snapshot() ：抓取指标，写 macro_indicators（最新快照）
                       与 macro_indicator_history（历史，读数变化才追加）。
                       scope="all" 全抓（手动刷新）；scope="auto" 按报告期决定
                       —— 日频每轮都抓、月频只在各自发布日之后才抓（见 monthly_plan）
- backfill_history() ：按（指标, 数据日期）回溯写入历史序列，幂等可重复执行；
                       历史表过少时自动升级为全量回溯。**只由「宏观趋势」页的
                       「补齐历史数据」按钮触发，不在定时任务里跑**

定时抓取由 CloudBase 云函数在北京时间 21:34 调用 POST /api/crawl/daily-refresh
点火（见 app/api/crawl.py 与 app/scheduler.py 的收盘轮）；定时任务只调用
refresh_snapshot() 抓当前读数，不做历史回溯。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import macro_backfill as mbf
from app.utils import macro_indicators as mi
from app.utils import macro_thermometer

logger = logging.getLogger(__name__)

# 历史表条数低于此值时，回填自动升级为全量窗口（新部署 / 历史被清空后自愈）
SPARSE_HISTORY_ROWS = 120

# 全量回溯窗口
FULL_WINDOWS = {"months_daily": 24, "months_monthly": 60, "tsf_limit": 13}

# 直接调用 backfill_history() 且不传窗口时的默认值（只补最近几个月，请求量小）。
# 页面按钮走 POST /api/macro/backfill，会显式传全量窗口（24 个月日度 / 60 个月月度）。
INCREMENTAL_WINDOWS = {"months_daily": 2, "months_monthly": 3, "tsf_limit": 3}

# ---------------------------------------------------------------- 月频抓取判定
#
# 用户 2026-11-01 要求：月度数据不要每轮都去试探，**只在各自发布日之后才拉**。
# 于是每项月频有一个「次月第几天起可抓」的日历（macro_indicators.MONTHLY_RELEASE_DAYS）：
#   PMI 次月 1 号（统计局当月最后一天就发布）｜PPI 次月 10 号｜
#   社融 / M1−M2 / 信贷结构 次月 16 号（央行次月 10~15 日发布）
#
# 判定 = 报告期 + 发布日两个条件同时满足才抓：
#   · 报告期已 ≥ 上月 → 不抓（本月已拿到）
#   · 报告期落后但还没到发布日 → 不抓，也不发请求（这就是「不每天试探」）
#   · 报告期落后且已过发布日 → 抓；抓不到保留旧读数，次日再来，直到拿到
#
# 为什么仍用「报告期」而不是「本月已抓」标记：数据本身就是状态，天然幂等，
# 清库 / 换库都不会丢判断依据。
#
# ⚠️ 不要改回"所有月频共用一个日子"：PMI 当月最后一天就发布，跟社融一起等到
# 16 号会让它白滞后半个月。各项错峰才能"发布完就拿到"。


def _period_of(as_of: Any) -> Optional[str]:
    """把 as_of 归一成 "YYYY-MM"；不是合规月度报告期（如「兜底默认值」）返回 None。"""
    text = str(as_of or "").strip()[:7]
    if len(text) != 7 or text[4] != "-":
        return None
    if not (text[:4].isdigit() and text[5:].isdigit()):
        return None
    return text


def _prev_month(today: date) -> str:
    """上一自然月的 "YYYY-MM"（月度数据次月发布，故期望报告期就是上月）。"""
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def expected_monthly_period(today: Optional[date] = None) -> str:
    """当前应有的最新月度报告期（"YYYY-MM"）。页面用来显示「月频数据到几月了」。"""
    return _prev_month(today or date.today())


def _release_date(period: str, day: int) -> date:
    """报告期 period（"YYYY-MM"）的发布日 = 次月第 day 天。

    例：period="2026-10"、day=16 → 2026-11-16（10 月社融在 11 月 16 号起可抓）。
    """
    year, month = int(period[:4]), int(period[5:7])
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(next_year, next_month, day)


def monthly_plan(db: Session, today: Optional[date] = None) -> Dict[str, Any]:
    """月频抓取计划（**纯读库判断**，不打任何外部请求）。

    每项月频数据只在它自己的发布日之后才去抓（见 macro_indicators.MONTHLY_RELEASE_DAYS），
    所以月初不会为了 1 项 PMI 把 6 个还没发布的接口全试一遍。

    返回：
      due          : 本轮是否有月频项该抓
      reason       : 一句可读原因（写 crawl_logs / 页面提示）
      expected     : 期望报告期 "YYYY-MM"（上月）
      pending      : [(key, 当前报告期)] 已过发布日但报告期还没到位 → 本轮抓
      waiting      : [(key, 发布日 date)] 还没到发布日 → 本轮连请求都不发
      targets      : 本轮该抓的月频键（= pending 的 key）
      next_release : 最近一次"等待中"的发布日，没有则 None（页面显示「等 X 号起抓」）
    """
    today = today or date.today()
    want = _prev_month(today)
    rows = crud.get_macro_indicators(db)
    pending: list = []
    waiting: list = []
    targets: list = []
    next_release: Optional[date] = None

    for key in mi.MONTHLY_KEYS:
        row = rows.get(key)
        period = _period_of(getattr(row, "as_of", None)) if row is not None else None
        if period is not None and period >= want:
            continue  # 已到位，本月不再抓这一项
        release = _release_date(want, mi.MONTHLY_RELEASE_DAYS.get(key, 1))
        if today < release:
            waiting.append((key, release))
            if next_release is None or release < next_release:
                next_release = release
        else:
            shown = period or getattr(row, "as_of", None) or "无"
            pending.append((key, shown))
            targets.append(key)

    if pending:
        reason = "待补齐至 %s：%s" % (
            want, ", ".join("%s=%s" % (k, v) for k, v in pending))
        due = True
    elif waiting:
        # 按发布日分组，避免把「11-10 才发的 PPI」和「11-16 才发的社融」写成同一天
        groups: Dict[date, list] = {}
        for key, release in waiting:
            groups.setdefault(release, []).append(key)
        reason = "；".join(
            "%s 起抓 %s" % (day.isoformat(), ", ".join(keys))
            for day, keys in sorted(groups.items())
        )
        due = False
    else:
        reason = "本月已齐（报告期均 ≥ %s）" % want
        due = False

    return {
        "due": due,
        "reason": reason,
        "expected": want,
        "pending": pending,
        "waiting": waiting,
        "targets": targets,
        "next_release": next_release,
    }


def monthly_due(db: Session, today: Optional[date] = None) -> Tuple[bool, str]:
    """本轮是否还需要抓月频。返回 (是否需要抓, 一句原因)。规则见 monthly_plan。"""
    plan = monthly_plan(db, today)
    return plan["due"], plan["reason"]


def anchor_value(db: Session) -> float:
    """当前 7 天逆回购利率锚（取库中已抓到的央行值，缺失时用兜底默认值）。"""
    row = crud.get_macro_indicator(db, mi.PARAM_REVERSE_REPO)
    if row is not None and row.value is not None:
        return float(row.value)
    return float(mi.PARAM_META[mi.PARAM_REVERSE_REPO]["default"])


def current_hints(db: Session) -> Dict[str, dict]:
    """各指标已落库的 detail，供央行侧判断「报告期未变则跳过」以省流量。"""
    rows = crud.get_macro_indicators(db)
    return {key: mi.load_detail(row.detail) for key, row in rows.items()}


def load_raw(db: Session) -> Dict[str, dict]:
    """读取库中各指标读数，组装成 build_items 需要的 raw 结构。

    与 app/api/macro.py 的 _load_raw 是同一份实现（那边改为转调本函数），
    保证「页面展示的温度计」与「落库打卡的温度计」读数完全一致。
    """
    raw: Dict[str, dict] = {}
    for key, row in crud.get_macro_indicators(db).items():
        if key == mi.PARAM_REVERSE_REPO:
            continue
        detail = mi.load_detail(row.detail)
        raw[key] = {
            "value": row.value,
            "as_of": row.as_of,
            "source": row.source,
            "source_detail": _source_note(detail),
            "detail": detail,
            "error": None,
        }

    # 未落库时用兜底默认值，保证温度计能算出来
    for key, meta in mi.FALLBACK_DEFAULTS.items():
        if key == mi.PARAM_REVERSE_REPO or key in raw:
            continue
        raw[key] = {
            "value": meta.get("value"),
            "as_of": meta.get("as_of", "兜底默认值"),
            "source": "fallback",
            "source_detail": "",
            "detail": {},
            "error": None,
        }
    return raw


def _source_note(detail: dict) -> str:
    """从 detail 里挑一句能说明数据出处的说明（如报告期 / 公告号）。"""
    for field in ("报告期", "报告", "公告", "来源"):
        val = detail.get(field)
        if val:
            return str(val)
    return ""


def _record_thermometer(db: Session) -> int:
    """把当前宏观温度计打卡写进历史表（key="macro_thermometer"），供趋势页画曲线。

    宏观温度计是**合成值**（资金面×25% + 实体热度×45% + 信用/部门×30%），
    不在任何一张指标表里，每次都现算。若不落库，趋势页就没有它的曲线。
    故这里按「每日收盘打卡」写一行：value = 总分，detail 里存三个子温度计的分数。

    as_of 用**打卡当天**（而非各分项的数据日期）—— 温度计是「当前合成读数」，
    与股市热度页的 key="temperature" 打卡口径一致；曲线因此是逐日的。
    """
    raw = load_raw(db)
    items = mi.build_items(raw)
    thermo = macro_thermometer.compute_thermometer(items)
    score = thermo.get("score")
    if score is None:
        return 0

    detail: Dict[str, Any] = {}
    for sub in thermo.get("subs") or []:
        if sub.get("score") is not None:
            detail[sub["key"]] = sub["score"]
    detail["spread"] = thermo.get("spread")
    detail["reading"] = thermo.get("reading")
    detail["数据源"] = thermo.get("formula")

    today = date.today().strftime("%Y-%m-%d")
    return crud.add_macro_history(db, [{
        "key": mi.THERMOMETER_KEY,
        "value": score,
        "as_of": today,
        "source": "akshare",
        "detail": mi.dump_detail(detail),
    }])


def refresh_snapshot(db: Optional[Session] = None,
                     scope: str = "all",
                     stats: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, str], int]:
    """抓取自动指标并落库。

    返回 (成功写入快照条数, {失败指标: 原因}, 新增历史记录条数)。

    抓取失败的指标保留上一次成功读数，不用空值覆盖。传入 db 时复用调用方会话
    （Web 接口走请求级 get_db），不传则自建并在结束时关闭（定时任务在请求上下文
    之外调用，无法依赖 FastAPI 的依赖注入）。

    scope —— 抓取范围（2026-11-01 新增，起因是「月度数据不必每天拉」）：
      · "all"（默认）：日频 + 月频全抓。手动刷新（页面「刷新数据」按钮）走这个，
        行为与改造前完全一致。
      · "auto"：**定时任务用**。按 macro_refresh.monthly_plan 判断——只抓日频 4 项
        加上"已过发布日、报告期还没到位"的月频项；未到发布日的月频项不发请求。
      · "daily" / "monthly"：显式限定，供排查用。

    stats —— 可选 dict，函数把本轮决策写进去（scope / monthly_reason），
    供定时任务写进 crawl_logs，线上排查能看到「这轮为什么只抓了日频」。
    不放进返回值是为了不破坏已有的三元组契约。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        reason = ""
        only_keys: Optional[set] = None
        if scope == "auto":
            plan = monthly_plan(db)
            reason = plan["reason"]
            # 只请求「日频 4 项 + 已过发布日且报告期还没到位的月频项」，
            # 没到发布日的月频项连请求都不发（这才是「不每天试探」）。
            only_keys = set(mi.DAILY_KEYS) | set(plan["targets"])
            scope = "all" if plan["targets"] else "daily"
        if stats is not None:
            stats["scope"] = scope
            stats["monthly_reason"] = reason
        logger.info("宏观抓取范围=%s（%s）", scope, reason or "显式指定")

        fetched = mi.fetch_auto_values(
            anchor_fallback=anchor_value(db),
            hints=current_hints(db),
            scope=scope,
            only_keys=only_keys,
        )

        records = []
        errors: Dict[str, str] = {}
        for key, item in fetched.items():
            if item.get("skipped"):
                continue  # 报告期未变，沿用库中读数
            if item.get("error"):
                errors[key] = item["error"]
                continue  # 保留上一次成功读数，不用空值覆盖
            records.append({
                "key": key,
                "value": item.get("value"),
                "as_of": item.get("as_of"),
                "source": item.get("source") or "akshare",
                "detail": mi.dump_detail(item.get("detail") or {}),
            })

        history_added = 0
        if records:
            crud.upsert_macro_indicators(db, records)
            history_added = crud.add_macro_history(db, records)

        # 宏观温度计是合成值（不在指标表里），单独打卡一行，
        # 趋势页才有它的曲线。放在指标落库之后 —— 温度计读的是**库中最新值**。
        history_added += _record_thermometer(db)

        logger.info(
            "市场指标自动刷新: 快照更新 %d 项, 历史新增 %d 条, 失败 %d 项",
            len(records), history_added, len(errors),
        )
        return len(records), errors, history_added
    finally:
        if own:
            db.close()


def backfill_history(
    db: Optional[Session] = None,
    months_daily: int = INCREMENTAL_WINDOWS["months_daily"],
    months_monthly: int = INCREMENTAL_WINDOWS["months_monthly"],
    tsf_limit: int = INCREMENTAL_WINDOWS["tsf_limit"],
    escalate_if_sparse: bool = True,
) -> Dict[str, Any]:
    """回溯历史序列并写入历史表（幂等：按「指标 + 数据日期」覆盖写）。

    触发入口只有页面「补齐历史数据」按钮（POST /api/macro/backfill）；**定时任务
    不再调用本函数**（见 app/scheduler.py，定时任务只抓当前读数）。不传窗口时走增量
    窗口（只补最近几个月，请求量小）；当历史表条数少于 SPARSE_HISTORY_ROWS
    （新部署、历史被清空）时自动升级为全量窗口。

    返回 {generated, added, updated, counts, errors, history_total, escalated, windows}。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        escalated = False
        if escalate_if_sparse and crud.count_macro_history(db) < SPARSE_HISTORY_ROWS:
            months_daily = FULL_WINDOWS["months_daily"]
            months_monthly = FULL_WINDOWS["months_monthly"]
            tsf_limit = FULL_WINDOWS["tsf_limit"]
            escalated = True
            logger.info("市场指标历史偏少，本次回填升级为全量窗口 (%s)", FULL_WINDOWS)

        result = mbf.backfill_all(months_daily, months_monthly, tsf_limit)

        records = []
        for series in (result.get("series") or {}).values():
            for rec in series:
                records.append({
                    "key": rec["key"],
                    "value": rec.get("value"),
                    "as_of": rec.get("as_of"),
                    "source": rec.get("source", "akshare"),
                    "detail": mi.dump_detail(rec.get("detail") or {}),
                })

        written = crud.bulk_upsert_macro_history(db, records)
        logger.info(
            "市场指标回溯: 生成 %d 条, 新增 %d, 更新 %d, 失败 %s",
            len(records), written["added"], written["updated"], result.get("errors"),
        )
        return {
            "generated": len(records),
            "added": written["added"],
            "updated": written["updated"],
            "counts": result.get("counts") or {},
            "errors": result.get("errors") or {},
            "history_total": crud.count_macro_history(db),
            "escalated": escalated,
            "windows": {
                "months_daily": months_daily,
                "months_monthly": months_monthly,
                "tsf_limit": tsf_limit,
            },
        }
    finally:
        if own:
            db.close()
