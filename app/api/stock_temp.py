"""股票指标（A 股冷热三层温度计）接口。

- GET  /api/stock-temp          读取当前指标、分层判读与综合温度计（**只读库，不再自行抓取**）
- POST /api/stock-temp/refresh  强制重新抓取全部指标（页面「刷新数据」按钮走这里）
- GET  /api/stock-temp/series   读取趋势图所需的历史序列 + 重建的月度温度轨迹
- GET  /api/stock-temp/history  查询任一指标的历史读数（只增不改的历史表）
- POST /api/stock-temp/backfill 一次性回溯补齐历史数据（让趋势图有数据可画）

13 项指标全部自动获取，无手工录入入口：
- 乐咕乐股：沪深300 PE/PB、破净率、公募仓位、市场宽度
- 交易所官网：两市成交额、全A换手率（成交额 ÷ 流通市值）
- 中债/新浪：10 年期国债收益率（合成 ERP）
- 东财数据中心：两融余额、重要股东增减持（产业资本净增持）
- 新浪/东财：宽基股票ETF份额、新基金发行、科创50 与创业板指

落库为「最新快照 + 历史」两张表（见 app/models.py 的 StockTempIndicator /
StockTempHistory）。抓取失败的指标保留上一次成功读数并在响应 errors 中提示。

**本页数据全部由定时任务抓取**：`./start.sh` 启动的 `run_scheduler.py` 在
**北京时间每天 20:30** 触发股票指标任务（抓取实现见 app/utils/stock_temp_refresh.py，与本文件的
POST /refresh 共用同一份）。GET 接口只读库、不触发任何外部请求，所以打开页面是瞬时的
（交易所日频数据逐日抓取一次要 5 分钟以上，绝不能挂在页面请求上）。

**「股票趋势」页的长历史序列不由定时任务生成**：定时任务只抓当前读数（顺带把读数
变化与每日综合温度追加进历史表），历史回溯完全靠页面「补齐历史数据」按钮按需触发
（POST /api/stock-temp/backfill）。
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils import stock_temp as st
from app.utils import stock_temp_refresh as srf

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

    只统计页面真正展示的指标键（st.AUTO_KEYS），避免把库里其他键算进来。
    """
    keys = set(st.AUTO_KEYS)
    stamps = [
        row.updated_at
        for key, row in crud.get_stock_temp_indicators(db).items()
        if key in keys and row.updated_at is not None
    ]
    return max(stamps) if stamps else None


def _local_str(ts: Optional[datetime]) -> str:
    """UTC 时间戳转北京时间字符串（库中时间戳为 UTC naive）。"""
    if ts is None:
        return "—"
    return ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _load_raw(db: Session) -> dict:
    """从库中读取各指标读数，供判定使用（未落库时用兜底参考值保证页面有内容）。"""
    raw = {}
    for key, row in crud.get_stock_temp_indicators(db).items():
        raw[key] = {
            "value": row.value,
            "as_of": row.as_of,
            "source": row.source,
            "detail": st.load_detail(row.detail),
            "error": None,
        }
    for key, meta in st.FALLBACK_DEFAULTS.items():
        if key in raw:
            continue
        raw[key] = {
            "value": meta.get("value"),
            "as_of": meta.get("as_of", "兜底参考值"),
            "source": "fallback",
            "detail": {},
            "error": None,
        }
    return raw


def _payload(db: Session, refreshed: int, errors: dict, history_added: int = 0) -> dict:
    raw = _load_raw(db)
    for key, msg in errors.items():
        if key in raw:
            raw[key]["error"] = msg
        elif key in st.INDICATOR_MAP:
            raw[key] = {
                "value": None, "as_of": "—", "source": "akshare",
                "detail": {}, "error": msg,
            }
    payload = st.build_response(raw)
    payload["refreshed"] = refreshed
    payload["errors"] = errors
    payload["history_added"] = history_added
    payload["history_total"] = crud.count_stock_temp_history(db)

    # updated_at 用「快照最近一次落库时间」而不是本次响应时间：本页读数由定时任务抓取
    # 写入，页面只是读库，报响应时间会让人误以为数据刚刚刷新过。
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


@router.get("/stock-temp")
def get_stock_temp(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """读取库中已有的三层指标读数、阈值判读与综合市场温度计（**只读**）。

    数据由 `./start.sh` 启动的定时任务在**北京时间每天 20:30** 抓取写入；需要立刻更新时
    走 POST /api/stock-temp/refresh（页面「刷新数据」按钮）。
    """
    try:
        crud.seed_stock_temp_history_from_snapshot(db)
        return _payload(db, 0, {})
    except Exception as e:
        logger.error("获取股票指标失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/stock-temp/refresh")
def refresh_stock_temp(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """强制重新抓取全部指标（与定时任务走同一实现）。"""
    try:
        crud.seed_stock_temp_history_from_snapshot(db)
        refreshed, errors, history_added = srf.refresh_snapshot(db)
        return _payload(db, refreshed, errors, history_added)
    except Exception as e:
        logger.error("刷新股票指标失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/stock-temp/series")
def get_stock_temp_series(
    per_key: int = 6000,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回趋势面板所需的历史序列，以及按月度重建的三层温度轨迹。

    - series      ：{指标键: [{as_of, value, source}, ...]}，时间正序
    - temperature ：{monthly: [{as_of, valuation, sentiment, capital, total}]}
    - counts      ：各指标可用点数，前端据此决定画什么
    """
    try:
        per_key = max(10, min(int(per_key or 6000), 20000))
        grouped = crud.get_stock_temp_history_series(db, st.CHART_KEYS, per_key_limit=per_key)

        series = {}
        counts = {}
        for key in st.CHART_KEYS:
            rows = grouped.get(key) or []
            pts = [
                {"as_of": r.as_of, "value": r.value, "source": r.source}
                for r in rows if r.value is not None and r.as_of
            ]
            series[key] = pts
            counts[key] = len(pts)

        temp = st.reconstruct_temperature({
            k: [{"as_of": p["as_of"], "value": p["value"]} for p in v]
            for k, v in series.items()
        })

        return {
            "status": "ok",
            "series": series,
            "temperature": temp,
            "counts": counts,
            "total": sum(counts.values()),
        }
    except Exception as e:
        logger.error("获取股票指标序列失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/stock-temp/history")
def get_stock_temp_history(
    key: Optional[str] = None,
    limit: int = 200,
    order: str = "desc",
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """查询股票指标历史读数。key 省略时返回全部指标的历史。"""
    try:
        limit = max(1, min(int(limit or 200), 20000))
        if key and key not in st.CHART_KEYS and key != "temperature":
            raise HTTPException(status_code=400, detail="未知指标: %s" % key)
        rows = crud.get_stock_temp_history(db, key=key, limit=limit, order=order)
        name_of = {ind["key"]: ind["name"] for ind in st.INDICATORS}
        name_of["temperature"] = "综合市场温度"
        name_of["total_float_mcap"] = "两市流通市值"
        records = []
        for row in rows:
            records.append({
                "key": row.key,
                "name": name_of.get(row.key, row.key),
                "value": row.value,
                "as_of": row.as_of,
                "source": row.source,
                "source_label": st.SOURCE_LABELS.get(row.source, row.source),
                "fetched_at": row.fetched_at.strftime("%Y-%m-%d %H:%M:%S") if row.fetched_at else None,
                "detail": st.load_detail(row.detail),
            })
        return {
            "status": "ok",
            "key": key,
            "order": order,
            "count": len(records),
            "total": crud.count_stock_temp_history(db, key=key),
            "records": records,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("查询股票指标历史失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/stock-temp/backfill")
def backfill_stock_temp(
    exchange_days: int = 60,
    industry_months: int = 24,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """一次性回溯补齐历史数据（让趋势图有数据可画）。

    可重复执行：按（指标, 数据日期）覆盖写入，不会堆重复行。
    交易所日频数据需逐日请求（每个交易日 2 次），exchange_days=60 约需 2~3 分钟；
    各数据源自带历史的指标（PE/PB/ERP/破净率/两融/公募仓位/新基金）几乎瞬时完成。
    """
    try:
        exchange_days = max(0, min(int(exchange_days or 60), 250))
        industry_months = max(1, min(int(industry_months or 24), 60))
        result = srf.backfill_history(
            db, exchange_days=exchange_days, industry_months=industry_months,
            escalate_if_sparse=False,
        )
        return {
            "status": "ok",
            "generated": result["generated"],
            "added": result["added"],
            "updated": result["updated"],
            "counts": result["counts"],
            "errors": result["errors"],
            "history_total": result["history_total"],
            "windows": result["windows"],
        }
    except Exception as e:
        logger.error("股票指标回溯失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
