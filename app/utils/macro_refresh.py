"""市场指标数据抓取与落库（供 API 与定时任务共用）。

把「抓取 → 落库」从接口层抽出来，让 Web 接口与后台定时任务用同一份实现，
避免两处逻辑漂移：

- refresh_snapshot() ：抓取全部自动指标，写 macro_indicators（最新快照）
                       与 macro_indicator_history（历史，读数变化才追加）
- backfill_history() ：按（指标, 数据日期）回溯写入历史序列，幂等可重复执行；
                       历史表过少时自动升级为全量回溯

定时任务在 app/scheduler.py 注册，由独立进程 run_scheduler.py 执行
（Web 进程默认不跑调度器，见 settings.START_SCHEDULER）。
"""
import logging
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import macro_backfill as mbf
from app.utils import macro_indicators as mi

logger = logging.getLogger(__name__)

# 历史表条数低于此值时，回填自动升级为全量窗口（新部署 / 历史被清空后自愈）
SPARSE_HISTORY_ROWS = 120

# 全量回溯窗口
FULL_WINDOWS = {"months_daily": 24, "months_monthly": 60, "tsf_limit": 13, "er_limit": 20}

# 日常增量回溯窗口（只补最近的数据，请求量小，适合每日定时执行）
# er_limit=0：超储率要逐份下载央行季报 PDF（每份数 MB），不放进日更
INCREMENTAL_WINDOWS = {"months_daily": 2, "months_monthly": 3, "tsf_limit": 3, "er_limit": 0}

# 超储率历史少于这个期数时，才在回填里顺带回溯一次（之后靠快照更新最新一期）
ER_MIN_ROWS = 4


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


def refresh_snapshot(db: Optional[Session] = None) -> Tuple[int, Dict[str, str], int]:
    """抓取全部自动指标并落库。

    返回 (成功写入快照条数, {失败指标: 原因}, 新增历史记录条数)。

    抓取失败的指标保留上一次成功读数，不用空值覆盖。传入 db 时复用调用方会话
    （Web 接口走请求级 get_db），不传则自建并在结束时关闭（定时任务在请求上下文
    之外调用，无法依赖 FastAPI 的依赖注入）。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        fetched = mi.fetch_auto_values(
            anchor_fallback=anchor_value(db),
            hints=current_hints(db),
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
    er_limit: Optional[int] = None,
    escalate_if_sparse: bool = True,
) -> Dict[str, Any]:
    """回溯历史序列并写入历史表（幂等：按「指标 + 数据日期」覆盖写）。

    默认走增量窗口（只补最近几个月，请求量小），适合每日定时执行；当历史表条数
    少于 SPARSE_HISTORY_ROWS（新部署、历史被清空）时自动升级为全量窗口，避免
    定时任务永远只补到最近几个月的数据。

    超储率（er_limit）单独处理：它只在央行季报 PDF 正文里，要逐份下载解析
    （每份数 MB、约 5~10 秒）。er_limit=None 时按「已有历史是否够」自动判断——
    不足 ER_MIN_ROWS 期才回溯一次；补过之后日更只靠快照写入最新一期，不再重复
    下载几十份 PDF。

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

        if er_limit is None:
            er_have = crud.count_macro_history(db, "excess_reserve")
            er_limit = FULL_WINDOWS["er_limit"] if er_have < ER_MIN_ROWS else 0
            if er_limit:
                logger.info("超储率历史仅 %d 期，本次回溯 %d 个季度", er_have, er_limit)

        result = mbf.backfill_all(months_daily, months_monthly, tsf_limit, er_limit)

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
                "er_limit": er_limit,
            },
        }
    finally:
        if own:
            db.close()
