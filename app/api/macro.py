"""市场指标（资金面 / 经济热度）接口。

- GET  /api/macro           读取当前指标与组合解读（缓存过期时自动抓取一次）
- POST /api/macro/refresh   强制重新抓取全部指标
- GET  /api/macro/history   查询各指标的历史读数（只增不改的历史表）
- GET  /api/macro/series    读取「三组一定位」图表面板所需的历史序列
- POST /api/macro/backfill  一次性回溯补齐历史数据（让趋势图有数据可画）

7 项指标全部自动获取，无任何手工录入入口：
- akshare：DR007 偏离、R001−DR001、PMI、M1−M2、PPI
- 央行官网（app/utils/pbc_data.py）：7天逆回购操作利率、社融存量同比、超储率

落库为「最新快照 + 历史」两张表：
- macro_indicators        ：每个指标一行，保存当前读数（覆盖式，页面读取用）
- macro_indicator_history ：每次抓到的读数发生变化就追加一行（只增不改，用于回溯）

抓取失败的指标保留上一次成功读数并在响应 errors 中提示，不会清空已有数据，
也不影响其他指标。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils import macro_backfill as mbf
from app.utils import macro_indicators as mi
from app.utils import macro_refresh as mrf

logger = logging.getLogger(__name__)

router = APIRouter()

# 仍有指标未拿到真实读数时的重试间隔（避免每次打开页面都阻塞在抓取上）
_RETRY_INTERVAL = timedelta(minutes=10)


def _utcnow() -> datetime:
    """与库表 server_default=func.now()（SQLite 的 CURRENT_TIMESTAMP，UTC）保持同一基准。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ensure_fresh(db: Session, force: bool = False) -> Tuple[int, Dict[str, str], int]:
    """缓存为空或已过期时抓取一次；force=True 时无条件抓取。

    返回 (快照更新条数, {失败指标: 原因}, 新增历史条数)。

    抓取实现见 app/utils/macro_refresh.py：该模块同时被定时任务复用
    （见 app/scheduler.py 的宏观指标任务）。定时任务已按配置周期主动刷新，
    这里的惰性抓取只作兜底：缓存过期或仍有指标没拿到真实读数时才触发。
    """
    if force:
        return mrf.refresh_snapshot(db)

    rows = crud.get_macro_indicators(db)
    # 仍未拿到真实读数的指标：库中缺失、来源为兜底默认值，或来源不在自动来源内
    # （含历史遗留的 manual 记录，会在本轮抓取中被真实读数替换）
    pending = [
        k for k in mi.AUTO_KEYS
        if k not in rows or (rows[k].source or "") not in mi.AUTO_SOURCES
    ]
    last_any = crud.get_macro_last_updated(db)

    if pending:
        # 抓不到真实读数时每次开页面都重试会阻塞，故限制重试间隔
        if last_any is not None and (_utcnow() - last_any) < _RETRY_INTERVAL:
            logger.info("市场指标仍缺真实读数 %s，距上次尝试不足 %s，跳过本次抓取",
                        pending, _RETRY_INTERVAL)
            return 0, {}, 0
        return mrf.refresh_snapshot(db)

    last = crud.get_macro_last_updated(db, source="akshare")
    ttl = timedelta(hours=settings.MACRO_CACHE_TTL_HOURS)
    if last is None or (_utcnow() - last) >= ttl:
        return mrf.refresh_snapshot(db)
    return 0, {}, 0


def _source_note(detail: dict) -> str:
    """从 detail 里挑一句能说明数据出处的说明（如报告期 / 公告号）。"""
    for field in ("报告期", "报告", "公告", "来源"):
        val = detail.get(field)
        if val:
            return str(val)
    return ""


def _load_raw(db: Session) -> Dict[str, dict]:
    """从库中读取各指标读数，供判定使用。"""
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

    # 未落库时用兜底默认值，保证页面有数可展示
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


def _load_params(db: Session) -> Dict[str, dict]:
    """读取 7 天逆回购利率锚（自动抓取自央行公告，只读展示）。"""
    meta = mi.PARAM_META[mi.PARAM_REVERSE_REPO]
    row = crud.get_macro_indicator(db, mi.PARAM_REVERSE_REPO)
    value = row.value if row is not None and row.value is not None else meta["default"]
    source = row.source if row is not None else "fallback"
    detail = mi.load_detail(row.detail) if row is not None else {}
    return {
        mi.PARAM_REVERSE_REPO: {
            "key": mi.PARAM_REVERSE_REPO,
            "name": meta["name"],
            "unit": meta["unit"],
            "digits": meta["digits"],
            "hint": meta["hint"],
            "value": value,
            "as_of": (row.as_of if row is not None else None) or "—",
            "source": source,
            "source_label": mi.SOURCE_LABELS.get(source, source),
            "source_detail": _source_note(detail),
        }
    }


def _payload(db: Session, refreshed: int, errors: Dict[str, str], history_added: int = 0) -> dict:
    raw = _load_raw(db)
    for key, msg in errors.items():
        if key in raw:
            raw[key]["error"] = msg
    payload = mi.build_response(raw, _load_params(db))
    payload["refreshed"] = refreshed
    payload["errors"] = errors
    payload["history_added"] = history_added
    payload["history_total"] = crud.count_macro_history(db)
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return payload


@router.get("/macro")
def get_macro(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """返回资金面 / 经济热度各项指标、状态判定、指标说明与组合解读。"""
    try:
        crud.seed_macro_fallbacks(db, mi.FALLBACK_DEFAULTS)
        crud.seed_macro_history_from_snapshot(db)
        refreshed, errors, history_added = _ensure_fresh(db)
        return _payload(db, refreshed, errors, history_added)
    except Exception as e:
        logger.error("获取市场指标失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/macro/refresh")
def refresh_macro(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """强制重新抓取全部指标（与定时任务走同一实现）。"""
    try:
        crud.seed_macro_fallbacks(db, mi.FALLBACK_DEFAULTS)
        crud.seed_macro_history_from_snapshot(db)
        refreshed, errors, history_added = mrf.refresh_snapshot(db)
        return _payload(db, refreshed, errors, history_added)
    except Exception as e:
        logger.error("刷新市场指标失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/macro/history")
def get_macro_history(
    key: Optional[str] = None,
    limit: int = 200,
    order: str = "desc",
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """查询市场指标历史读数（只增不改的历史表）。

    - key   ：指标键；不传则返回全部指标的历史（如 dr007_spread / excess_reserve /
              r001_dr001 / pmi / tsf_yoy / m1_m2 / ppi_yoy / reverse_repo_rate）
    - limit ：返回条数，1~2000，默认 200
    - order ：desc（默认，最新在前）/ asc
    """
    try:
        limit = max(1, min(int(limit or 200), 2000))
        if key and key not in mi.HISTORY_KEYS:
            raise HTTPException(status_code=400, detail="未知指标: %s" % key)
        rows = crud.get_macro_history(db, key=key, limit=limit, order=order)

        name_of = mi.NAME_OF_KEY

        records = []
        for row in rows:
            records.append({
                "key": row.key,
                "name": name_of.get(row.key, row.key),
                "value": row.value,
                "as_of": row.as_of,
                "source": row.source,
                "source_label": mi.SOURCE_LABELS.get(row.source, row.source),
                "fetched_at": row.fetched_at.strftime("%Y-%m-%d %H:%M:%S") if row.fetched_at else None,
                "detail": mi.load_detail(row.detail),
            })
        return {
            "status": "ok",
            "key": key,
            "order": order,
            "count": len(records),
            "total": crud.count_macro_history(db, key=key),
            "records": records,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("查询市场指标历史失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/macro/series")
def get_macro_series(
    per_key: int = 3000,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回「三组一定位」图表面板所需的全部历史序列。

    - daily  ：日度序列（DR007 偏离、R001 − DR001），用于偏离度图与分层利差图
    - monthly：月度序列（PMI、PPI、社融存量同比、M1/M2 同比、M1−M2 剪刀差），
               用于阈值带图、双线剪刀差图、归一化叠加与四象限轨迹
    - anchor_steps：7 天逆回购操作利率的历史档位（回溯 DR007 偏离时的锚）

    序列一律按时间正序返回，元素为 {as_of, value, source}。
    """
    try:
        per_key = max(10, min(int(per_key or 3000), 5000))
        grouped = crud.get_macro_history_series(db, mi.CHART_KEYS, per_key_limit=per_key)

        def _pack(key: str):
            return [
                {"as_of": r.as_of, "value": r.value, "source": r.source}
                for r in grouped.get(key) or []
                if r.value is not None
            ]

        daily = {k: _pack(k) for k in mi.CHART_DAILY_KEYS}
        monthly = {k: _pack(k) for k in mi.CHART_MONTHLY_KEYS}

        counts = {k: len(v) for k, v in list(daily.items()) + list(monthly.items())}
        return {
            "status": "ok",
            "daily": daily,
            "monthly": monthly,
            "anchor_steps": [{"from": d, "rate": r} for d, r in mbf.POLICY_RATE_STEPS],
            "counts": counts,
            "total": sum(counts.values()),
        }
    except Exception as e:
        logger.error("获取市场指标序列失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/macro/backfill")
def backfill_macro(
    months_daily: int = 24,
    months_monthly: int = 60,
    tsf_limit: int = 13,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """一次性回溯补齐历史数据（日度均值/月度序列），写入历史表。

    这是「让趋势图有数据可画」的动作，可重复执行：按（指标, 数据日期）覆盖写入，
    不会堆重复行。首次执行约需 1~2 分钟（日度区间逐月请求 + 央行社融逐期解析）。

    定时任务每天会用较小的增量窗口自动补最近数据（见 app/scheduler.py），
    这个接口用于需要立刻全量补齐时手动触发。
    """
    try:
        months_daily = max(1, min(int(months_daily or 24), 60))
        months_monthly = max(6, min(int(months_monthly or 60), 240))
        tsf_limit = max(1, min(int(tsf_limit or 13), 40))

        # 手动触发即按请求窗口执行，不做「历史偏少则升级全量」的自动判断
        # （调用方已明确给出窗口，尊重其意图）
        result = mrf.backfill_history(
            db, months_daily, months_monthly, tsf_limit, escalate_if_sparse=False
        )

        return {
            "status": "ok",
            "generated": result["generated"],
            "added": result["added"],
            "updated": result["updated"],
            "counts": result["counts"],
            "errors": result["errors"],
            "history_total": result["history_total"],
            "months_daily": months_daily,
            "months_monthly": months_monthly,
        }
    except Exception as e:
        logger.error("市场指标回溯失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
