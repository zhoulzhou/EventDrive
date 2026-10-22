"""股票指标（A 股冷热三层温度计）接口。

- GET  /api/stock-temp          读取当前指标、分层判读与综合温度计（**只读库，不再自行抓取**）
- POST /api/stock-temp/refresh  强制重新抓取全部指标（页面「刷新数据」按钮走这里）
- GET  /api/stock-temp/series   读取趋势图所需的历史序列 + 每日综合温度打卡
- GET  /api/stock-temp/history  查询任一指标的历史读数（只增不改的历史表）
- POST /api/stock-temp/backfill 回溯补齐历史（**页面已无入口**，仅备必要时手动调用）

13 项指标全部自动获取，无手工录入入口：
- 乐咕乐股：沪深300 PE/PB、破净率、公募仓位、市场宽度
- 交易所官网：两市成交额、全A换手率（成交额 ÷ 流通市值）
- 中债/新浪：10 年期国债收益率（合成 ERP）
- 东财数据中心：两融余额、重要股东增减持（产业资本净增持）
- 新浪/东财：宽基股票ETF份额、新基金发行、科创50 与创业板指

落库为「最新快照 + 历史」两张表（见 app/models.py 的 StockTempIndicator /
StockTempHistory）。抓取失败的指标保留上一次成功读数并在响应 errors 中提示。

**本页数据全部由定时任务抓取**：定时任务在**北京时间每天 16:00** 触发股票指标任务
（抓取实现见 app/utils/stock_temp_refresh.py，与本文件的 POST /refresh 共用同一份）。
GET 接口只读库、不触发任何外部请求，所以打开页面是瞬时的（交易所日频数据逐日抓取一次
要5 分钟以上，绝不能挂在页面请求上）。

**「热度趋势」页的数据从现在开始逐日积累，不做历史回溯**：定时任务每次抓取都会把各
指标读数与当日综合温度打卡（key="temperature"，detail 含三层温度与分离度）写进历史表，
/series 只读库返回并按日画曲线。页面已删除「补齐历史数据」按钮，也没有实时重建逻辑 ——
曲线长短取决于启用天数，这是预期行为。
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils import greed_index as greed_index_mod
from app.utils import stock_temp as st
from app.utils import stock_temp_refresh as srf

logger = logging.getLogger(__name__)

router = APIRouter()

# 页面「更新于」的展示时区（库中时间戳是 UTC，见 _utcnow）
CN_TZ = ZoneInfo("Asia/Shanghai")

# 超过这个时长没有落库就提示检查调度器。数据抓取任务每天只在**北京 16:00** 跑一次，
# 正常间隔就是 24 小时，故阈值取 26 小时（留 2 小时缓冲）：既避免误报，
# 又能在「某天没跑成」的次日晚上及时提示。
DATA_STALE_HOURS = 26

# ---------------------------------------------------------------- 后台刷新
#
# 云沙箱反代硬超时 60s，而带限流节流的完整抓取要 60s+，同步执行必然 504。
# 因此「刷新数据」按钮改为：点火后台线程 → 立即返回 → 页面下次读库即为新数据。
# _refresh_status 让页面能查询「是否还在跑 / 上次结果如何」。
_refresh_status: Dict[str, Any] = {"running": False, "last_result": None}


def _kick_stock_temp_refresh() -> bool:
    """启动一个后台刷新线程。返回 False 表示已有任务在跑（未重复启动）。"""
    if _refresh_status.get("running"):
        return False
    _refresh_status["running"] = True
    _refresh_status["started_at"] = datetime.now().isoformat()
    _refresh_status.pop("error", None)

    def _worker():
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            crud.seed_stock_temp_history_from_snapshot(db)
            refreshed, errors, history_added = srf.refresh_snapshot(db)
            payload = _payload(db, refreshed, errors, history_added)
            _refresh_status["last_result"] = {
                "refreshed": refreshed,
                "errors": errors or {},
                "history_added": history_added,
                "thermometer": (payload.get("thermometer") or {}).get("value"),
                "greed_index": (payload.get("greed_index") or {}).get("value"),
            }
            logger.info("股票指标后台刷新完成: %d 项, 失败 %d 项",
                        refreshed, len(errors or {}))
        except Exception as e:
            logger.error("后台刷新股票指标失败: %s", e, exc_info=True)
            _refresh_status["error"] = str(e)
        finally:
            try:
                db.close()
            except Exception:
                pass
            _refresh_status["running"] = False
            _refresh_status["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=_worker, name="stock-temp-refresh", daemon=True).start()
    return True


@router.get("/stock-temp/refresh-status")
def refresh_status(auth: bool = Depends(require_auth)):
    """查询后台刷新进度（running / 上次结果 / 错误）。"""
    return dict(_refresh_status)


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

    # 市场贪婪指数的 5 日移动平均（去极值，可选项）：由历史打卡点现算，
    # 样本不足 5 期时为 null，页面会注明「未平滑」。
    try:
        rows = crud.get_stock_temp_history(db, key="greed_index", limit=5, order="desc")
        hist = [r.value for r in rows if r.value is not None]
        cur = (payload.get("greed_index") or {}).get("value")
        seq = list(reversed(hist))
        if cur is not None:
            seq.append(cur)
        payload["greed_index"]["ma5"] = greed_index_mod.moving_average(seq, greed_index_mod.SMOOTH_DAYS)
    except Exception as e:
        logger.warning("计算贪婪指数 MA5 失败: %s", e)
        payload["greed_index"]["ma5"] = None

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

    数据由 `./start.sh` 启动的定时任务在**北京时间每天 16:00** 抓取写入；需要立刻更新时
    走 POST /api/stock-temp/refresh（页面「刷新数据」按钮）。
    """
    try:
        crud.seed_stock_temp_history_from_snapshot(db)
        return _payload(db, 0, {})
    except Exception as e:
        logger.error("获取股票指标失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/stock-temp/refresh")
async def refresh_stock_temp(auth: bool = Depends(require_auth)):
    """强制重新抓取全部指标。

    **立即返回、后台执行**：legulegu 有速率限制（实测连续 6~7 次请求即 429），
    为躲开限流，5 个 legulegu 接口必须串行且保持 ≥6s 间隔，一轮完整抓取约 60s+
    （含 akshare 内部每次为拿 csrf 额外发的页面请求）。而云沙箱的反向代理
    **硬超时 60 秒**，同步执行必然 504。

    所以这里只「点火」就返回：落库由后台线程完成，页面下次请求即可读到新数据
    （updated_at 会刷新，可据此判断是否已跑完）。
    """
    started = _kick_stock_temp_refresh()
    if not started:
        return {
            "status": "running",
            "message": "已有一个刷新任务在执行中，请稍候（上一轮通常需 1 分钟左右）",
        }
    return {
        "status": "started",
        "message": "已开始后台抓取，约 1 分钟后完成；页面会自动显示新读数",
    }


@router.post("/stock-temp/refresh-sync")
def refresh_stock_temp_sync(db: Session = Depends(get_db),
                            auth: bool = Depends(require_auth)):
    """同步刷新（供本地/脚本调用）。⚠ 线上有 60s 网关超时，线上别用这个。

    保留它是为了本地调试与「补齐历史」等需要拿到完整结果的场景。
    """
    try:
        crud.seed_stock_temp_history_from_snapshot(db)
        refreshed, errors, history_added = srf.refresh_snapshot(db)
        payload = _payload(db, refreshed, errors, history_added)
        _refresh_status.update({
            "running": False, "last_result": payload,
            "finished_at": datetime.now().isoformat(),
        })
        return payload
    except Exception as e:
        logger.error("刷新股票指标失败: %s", e, exc_info=True)
        _refresh_status.update({"running": False, "error": str(e)})
        return {"status": "error", "message": str(e)}


@router.post("/stock-temp/probe")
def probe_sources(auth: bool = Depends(require_auth)):
    """诊断用：直接探测各外部数据源在本运行环境下的可达性。

    「刷新数据」报legulegu 相关错误时，用它区分两种可能：
      - status 非 200 / 连不上      → 网络或出口 IP 被拦
      - 200 但 csrf=false           → 上游返回了拦截页（改版或风控）
    不写入任何数据，纯只读诊断。
    """
    import requests
    from akshare.utils.cons import headers

    targets = [
        ("legulegu/shanghaiPE", "https://legulegu.com/stockdata/shanghaiPE"),
        ("legulegu/sz50-ttm-lyr", "https://legulegu.com/stockdata/sz50-ttm-lyr"),
        ("legulegu/pos-stock", "https://legulegu.com/stockdata/fund-position/pos-stock"),
        ("sina/hs300", "https://hq.sinajs.cn/list=sh000300"),
    ]
    out = []
    for name, url in targets:
        item = {"name": name, "url": url}
        try:
            r = requests.get(url, headers=headers, timeout=20)
            item["status"] = r.status_code
            item["bytes"] = len(r.text)
            item["csrf"] = "_csrf" in r.text
            item["head"] = r.text[:120].replace("\n", " ")
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        out.append(item)
    return {"status": "ok", "results": out}


@router.get("/stock-temp/series")
def get_stock_temp_series(
    per_key: int = 6000,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回趋势面板所需的历史序列，以及**从历史表读取的每日综合温度**。

    - series      ：{指标键: [{as_of, value, source}, ...]}，时间正序
    - temperature ：{daily: [{as_of, total, valuation, sentiment, capital, spread}]}
    - counts      ：各指标可用点数，前端据此决定画什么

    「热度趋势」页只展示各指标曲线，数据**全部来自历史表**、从现在开始逐日积累
    （定时任务每天收盘抓一次写入），不再做历史回溯、不再实时重建。
    综合温度取每次刷新写入的打卡记录（key="temperature"），三层温度从其 detail 解析。
    """
    try:
        per_key = max(10, min(int(per_key or 6000), 20000))
        chart_keys = list(st.CHART_KEYS) + ["temperature"]
        grouped = crud.get_stock_temp_history_series(db, chart_keys, per_key_limit=per_key)

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

        daily = []
        for r in grouped.get("temperature") or []:
            if r.value is None or not r.as_of:
                continue
            d = st.load_detail(r.detail) or {}
            row = {"as_of": r.as_of, "total": r.value}
            for field in st.TEMP_TRACK_KEYS:
                if field == "total":
                    continue
                v = d.get(st.TEMP_LAYER_NAMES.get(field, ""))
                row[field] = v if isinstance(v, (int, float)) else None
            spread = d.get("三层分离度(pp)")
            row["spread"] = spread if isinstance(spread, (int, float)) else None
            daily.append(row)

        return {
            "status": "ok",
            "series": series,
            "temperature": {
                "daily": daily,
                "note": (
                    "综合温度为每次收盘刷新时写入的打卡记录，三层温度取自同一记录的明细。"
                    "自启用起逐日积累，样本少时仅供参考。"
                ),
            },
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
        name_of["greed_index"] = "市场贪婪指数"
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
    """回溯补齐历史数据。

    ⚠️ **「热度趋势」页已改为「只展示从现在开始积累的曲线」，页面不再提供该入口**
    （原「补齐历史数据」按钮已删除）。保留本接口仅为必要时手动补数据用；正常情况下
    历史数据由定时任务每天收盘抓取时逐日写入，不需要回溯。

    可重复执行：按（指标, 数据日期）覆盖写入，不会堆重复行。
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
