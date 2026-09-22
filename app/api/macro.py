"""宏观盯盘清单接口（三个维度 / 9 项指标）。

- GET  /api/macro           读取当前指标与组合解读（**只读库，不再自行抓取**）
- POST /api/macro/refresh   强制重新抓取全部指标（页面「刷新数据」按钮走这里）
- GET  /api/macro/history   查询各指标的历史读数（只增不改的历史表）
- GET  /api/macro/series    读取「三组一定位」图表面板所需的历史序列
- POST /api/macro/backfill  一次性回溯补齐历史数据（让趋势图有数据可画）

三个维度（每维 3 项），全部自动获取、无手工录入：
- ① 资金面      akshare：DR007 偏离、R001−DR001；央行：超储率
- ② 经济热度    akshare：PMI、PPI；央行：社融存量同比
- ③ 部门结构    akshare：M1−M2；央行《金融统计数据报告》：住户存款/贷款、非银存款

A股估值/情绪类读数不在本页，见「股票指标」页（/stock-temp）。

落库为「最新快照 + 历史」两张表：
- macro_indicators        ：每个指标一行，保存当前读数（覆盖式，页面读取用）
- macro_indicator_history ：每次抓到的读数发生变化就追加一行（只增不改，用于回溯）

**本页数据全部由定时任务抓取**：`./start.sh` 启动的 `run_scheduler.py` 在
**北京时间每天 20:30** 触发宏观指标任务（抓取实现见 app/utils/macro_refresh.py，与本文件的
POST /refresh 共用同一份）。GET 接口只读库、不触发任何外部请求，所以打开页面是瞬时的；
要立刻更新就点页面的「刷新数据」按钮（POST /refresh）。

**「宏观趋势」页的长历史序列不由定时任务生成**：定时任务只抓当前读数（顺带把读数
发生变化的行追加进历史表），历史回溯完全靠页面「补齐历史数据」按钮按需触发
（POST /api/macro/backfill）。

抓取失败的指标保留上一次成功读数并在响应 errors 中提示，不会清空已有数据，
也不影响其他指标。
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils import macro_backfill as mbf
from app.utils import macro_indicators as mi
from app.utils import macro_refresh as mrf

logger = logging.getLogger(__name__)

router = APIRouter()

# 页面「更新于」的展示时区（库中时间戳是 UTC，见 _utcnow）
CN_TZ = ZoneInfo("Asia/Shanghai")

# 超过这个时长没有落库就提示检查调度器。数据抓取任务每天只在**北京 20:30** 跑一次，
# 正常间隔就是 24 小时，故阈值取 26 小时（留 2 小时缓冲）：既避免误报，
# 又能在「某天没跑成」的次日晚上及时提示。
DATA_STALE_HOURS = 26


def _utcnow() -> datetime:
    """与库表 server_default=func.now()（SQLite 的 CURRENT_TIMESTAMP，UTC）保持同一基准。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _data_written_at(db: Session) -> Optional[datetime]:
    """本页展示的指标最近一次落库时间（UTC，可能为 None）。

    只统计页面真正展示的自动指标键（mi.AUTO_KEYS）：库里可能残留已删功能的孤儿行
    （如早先的 astock_* 读数），不带键过滤会把它们的时间算进来。
    """
    keys = set(mi.AUTO_KEYS)
    stamps = [
        row.updated_at
        for key, row in crud.get_macro_indicators(db).items()
        if key in keys and row.updated_at is not None
    ]
    return max(stamps) if stamps else None


def _local_str(ts: Optional[datetime]) -> str:
    """UTC 时间戳转北京时间字符串（库中时间戳为 UTC naive）。"""
    if ts is None:
        return "—"
    return ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


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

    # updated_at 用「快照最近一次落库时间」而不是本次响应时间：本页的读数由定时任务
    # 抓取写入，页面只是读库，报响应时间会让人误以为数据刚刚刷新过。
    written = _data_written_at(db)
    payload["updated_at"] = _local_str(written)
    if written is None:
        payload["data_age_hours"] = None
        payload["stale"] = False
    else:
        age = (_utcnow() - written).total_seconds() / 3600
        payload["data_age_hours"] = round(age, 1)
        payload["stale"] = age >= DATA_STALE_HOURS
    return payload


@router.get("/macro")
def get_macro(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """读取库中已有的指标与组合解读（**只读**，不触发抓取）。

    数据由 `./start.sh` 启动的定时任务在**北京时间每天 20:30** 抓取写入；需要立刻更新时
    走 POST /api/macro/refresh（页面「刷新数据」按钮）。
    """
    try:
        crud.seed_macro_fallbacks(db, mi.FALLBACK_DEFAULTS)
        crud.seed_macro_history_from_snapshot(db)
        return _payload(db, 0, {})
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

    **这是「宏观趋势」页历史数据的唯一入口**：定时任务只抓当前读数、不做回溯
    （见 app/scheduler.py），所以趋势图的长序列完全由本接口按需补齐。
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
