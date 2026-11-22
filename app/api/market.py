import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app import crud
from app.crawlers.market_data import refresh_market_data
from app.market_strategy import compute_drawdown, compute_drawdown_events, compute_level, get_strategy
from app.utils.index_swing import compute_swing
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()

# 固定的卡片显示顺序，独立于抓取列表 SERIES；后续新增指数在此追加，不影响历史顺序
# GOLD/BTC 为 7x24 品种，同页展示但不受美股交易日约束
DISPLAY_ORDER = ["NASDAQ100", "VIXCLS", "DGS2", "DGS10", "GOLD", "BTC"]

# 各指标的实际数据源文案（_to_item 里原本写死 FRED，黄金/比特币来自新浪）
SOURCE_LABELS = {"GOLD": "新浪财经", "BTC": "新浪财经"}

# 列表行着色口径：当前值距「区间最高点」的回撤幅度（百分数）。
# 阈值取自各指标近 1~3 个月历史回撤的 P25/P10 并取整，纳指100 沿用已有策略 5%/10%。
# 写死在配置里、不随每次新数据重算分位，避免颜色来回漂移；每月复核一次即可。
# VIX 是恐慌指数，回撤语义相反（回撤=恐慌缓解），继续走绝对值 20/30，不在此表。
DRAWDOWN_LEVELS = {
    "NASDAQ100": (5.0, 10.0),
    "DGS2": (3.0, 6.0),
    "DGS10": (4.0, 7.0),
    "GOLD": (7.0, 11.0),
    "BTC": (20.0, 50.0),
}


def _valid_points(rows) -> list:
    """过滤 date 为空的历史残留行，返回 [(date, value), ...]。"""
    out = []
    for r in rows or []:
        d = str(r.date)[:10] if r.date is not None else ""
        if r.value is not None and len(d) == 10 and d[4:5] == "-":
            out.append((d, float(r.value)))
    return out


def _drawdown_from_peak(value: Optional[float], rows) -> dict:
    """当前值相对区间最高点的回撤幅度与峰值信息。

    历史不足 2 个点（如 BTC 刚上线）时不下结论，保持常态——
    只有一个点时峰值就是当前值，回撤恒为 0，会把「无数据」误判成「安全」。
    """
    result = {"drawdown_pct": None, "drawdown_peak": None, "drawdown_peak_date": None}
    pts = _valid_points(rows)
    if value is None or len(pts) < 2:
        return result
    peak_date, peak = max(pts, key=lambda x: x[1])
    if peak <= 0:
        return result
    result["drawdown_pct"] = (peak - float(value)) / peak * 100
    result["drawdown_peak"] = peak
    result["drawdown_peak_date"] = peak_date
    return result


def _to_item(latest: dict) -> dict:
    """由最新两条历史记录构造带涨跌幅的展示项。"""
    row = latest["current"]
    previous = latest.get("previous")
    change = None
    change_percent = None
    if row.value is not None and previous is not None and previous.value:
        change = row.value - previous.value
        change_percent = (change / previous.value) * 100
    return {
        "name": row.name,
        "symbol": row.symbol,
        "unit": row.unit or "",
        "value": row.value,
        "change": change,
        "change_percent": change_percent,
        "date": row.date,
        "source": SOURCE_LABELS.get(row.symbol, "FRED"),
    }


@router.get("/market")
async def get_market_data(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """返回纳斯达克指数、VIX、美债2年期/10年期、黄金、比特币的当前值。

    列表行着色口径（2026-11-22 用户确认）：当前值相对**区间最高点的回撤**，
    各指标阈值见 DRAWDOWN_LEVELS；VIX 是恐慌指数，回撤语义相反，仍按绝对值
    20/30 分级。旧的 peak_* 字段与纳指100 的 swing 说明保留供排障，但不再
    决定行色；曲线上的每档回撤事件仍走 /market/history。
    """
    try:
        latest = crud.get_latest_market_prices(db)
        # 首次部署尚未有定时数据时，兜底实时抓取一次并落库
        if not latest:
            db.close()
            await refresh_market_data()
            db = SessionLocal()
            latest = crud.get_latest_market_prices(db)

        items = []
        for sym, rows in latest.items():
            items.append(_to_item({"current": rows[0], "previous": rows[1] if len(rows) > 1 else None}))

        # 仅展示 DISPLAY_ORDER 中的指标（过滤历史残留 symbol），并按固定顺序排序
        order = {sym: i for i, sym in enumerate(DISPLAY_ORDER)}
        items = [it for it in items if it["symbol"] in order]
        items.sort(key=lambda it: order[it["symbol"]])

        # 历史一次性批量取（云库下逐个取是 N 次往返，每次 0.25~0.9s）：
        # 既用于区间高点回撤分级，也用于纳指100 的区间摆动说明。
        hist_map = crud.get_market_history_multi(db, [it["symbol"] for it in items])
        # 旧的运行峰值状态仍批量取，peak_* 字段保留供排障，但不再决定行色。
        strategy_states = crud.get_market_strategy_states(
            db, [it["symbol"] for it in items if it["symbol"] != "VIXCLS"]
        )

        # 纳指100 的区间摆动说明保留（双向、不重置），供列表里看相对高/低点偏离；
        # 但行色统一改由「距区间高点回撤」决定——否则上涨偏离也会被染成红色。
        nasdaq_swing = None
        nd_strategy = get_strategy("NASDAQ100")
        if nd_strategy and nd_strategy.get("red_pct") is not None:
            red_pct = float(nd_strategy["red_pct"])
            green_pct = float(nd_strategy.get("green_pct", red_pct / 2))
            pts = [{"date": d, "value": v} for d, v in _valid_points(hist_map.get("NASDAQ100"))]
            if pts and red_pct > 0:
                full = compute_swing(
                    pts, red_pct,
                    warn_ratio=green_pct / red_pct,
                    ref_labels=("区间最高", "区间最低"),
                )
                nasdaq_swing = {
                    "threshold_pct": full["threshold_pct"],
                    "current": full["current"],
                    "high": full["high"],
                    "low": full["low"],
                }

        for it in items:
            if it["value"] is None:
                it["drawdown_level"] = "normal"
                continue
            # 旧的运行峰值信息（peak_*）保留供排障，但不决定行色
            if it["symbol"] != "VIXCLS":
                state = strategy_states.get(it["symbol"])
                it.update(compute_drawdown(it["symbol"], it["value"], state,
                                           current_date=it["date"]))

            if it["symbol"] == "VIXCLS":
                strategy = get_strategy(it["symbol"])
                thresholds = strategy.get("thresholds", [20, 30]) if strategy else [20, 30]
                it["level_thresholds"] = thresholds
                it.update(compute_level(it["value"], thresholds))
                continue

            if it["symbol"] == "NASDAQ100" and nasdaq_swing:
                it["swing"] = nasdaq_swing

            if it["symbol"] in DRAWDOWN_LEVELS:
                warn, red = DRAWDOWN_LEVELS[it["symbol"]]
                dd_info = _drawdown_from_peak(it["value"], hist_map.get(it["symbol"]))
                it.update(dd_info)
                it["drawdown_thresholds"] = [warn, red]
                dd = dd_info["drawdown_pct"]
                if dd is None:          # 历史点不足（如 BTC 首日）：不下结论，保持常态
                    it["drawdown_level"] = "normal"
                elif dd >= red:
                    it["drawdown_level"] = "danger"
                elif dd >= warn:
                    it["drawdown_level"] = "warn"
                else:
                    it["drawdown_level"] = "ok"

        return {
            "status": "ok",
            "items": items,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "Nasdaq / CBOE / 美国财政部 / COMEX黄金 / 比特币（腾讯·东财·新浪·FRED 兜底）",
        }
    except Exception as e:
        logger.error(f"获取市场数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/market/sources")
async def probe_market_sources(auth: bool = Depends(require_auth)):
    """体检：把每个指标的每个数据源都真打一次，返回能否取到、最新日期、耗时、错误。

    市场行情「某个指数不动」时先看这里：主源挂了会显示 error，
    备用源能取到数据就说明降级链能兜住（定时任务会自动走备用源）。
    """
    try:
        from app.crawlers.market_data import probe_sources
        return {"status": "ok", **(await probe_sources())}
    except Exception as e:
        logger.error(f"数据源体检失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/market/refresh")
async def refresh_market(
    force: bool = Query(True, description="true=强制走全部来源补漏，false=已最新则跳过"),
    rebuild_peak: bool = Query(False, description="true=按完整历史重算峰值回撤状态"),
    auth: bool = Depends(require_auth),
):
    """手动补跑市场行情（含补历史缺口），同步返回结果，便于排障后立即验证。"""
    try:
        from app.crawlers.market_data import refresh_market_data
        data = await refresh_market_data(force=force)
        rebuilt = None
        if rebuild_peak:
            from app.database import SessionLocal
            from app.market_strategy import rebuild_state
            from app.crawlers.market_data import SERIES as _SERIES
            db = SessionLocal()
            try:
                rebuilt = {
                    s["symbol"]: rebuild_state(db, s["symbol"])
                    for s in _SERIES
                }
            finally:
                db.close()
        return {
            "status": "ok",
            "skipped": data.get("skipped", False),
            "reason": data.get("reason"),
            "expected": data.get("expected"),
            "source": data.get("source"),
            "summary": data.get("summary"),
            "rebuilt_peak": rebuilt,
            "items": [
                {"symbol": it["symbol"], "name": it["name"], "value": it.get("value"),
                 "date": it.get("date"), "points": len(it.get("points") or []),
                 "sources": it.get("sources")}
                for it in data.get("items", [])
            ],
        }
    except Exception as e:
        logger.error(f"手动补跑市场行情失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/market/history")
async def get_market_history(
    symbol: str = Query(..., description="FRED Series ID，如 NASDAQ100/VIXCLS/DGS2/DGS10"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回某个指数/收益率的历史序列（按日期升序），用于绘制曲线图。

    若该 symbol 配置了峰值回撤策略，则同时返回 drawdown_events（10% 回撤事件的
    峰值日期/值与低点日期/值），供前端在图表中标记。
    """
    try:
        rows = crud.get_market_history(db, symbol)
        if not rows:
            return {"status": "ok", "symbol": symbol, "name": symbol, "unit": "", "points": [],
                    "drawdown_events": []}
        points = [{"date": r.date, "value": r.value} for r in rows]

        drawdown_events = []
        strategy = get_strategy(symbol)
        # 仅当配置了峰值回撤策略（red_pct）时才计算回撤事件；
        # 仅做分级的指标（如 VIXCLS 只有 thresholds）不参与，避免 KeyError 及误标事件
        if strategy is not None and strategy.get("red_pct") is not None:
            drawdown_events = compute_drawdown_events(points, strategy["red_pct"])

        return {
            "status": "ok",
            "symbol": symbol,
            "name": rows[0].name,
            "unit": rows[0].unit or "",
            "points": points,
            "drawdown_events": drawdown_events,
        }
    except Exception as e:
        logger.error(f"获取历史数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/market/cleanup")
async def cleanup_market_data(
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """手动清理稀疏日期：删除当日 4 个行情指标中非空值数量 < 3 的行。

    用于减少历史走势曲线的断点；预计低频触发，仅在有需要时由用户手动调用。
    """
    try:
        deleted = crud.cleanup_sparse_market_dates(db, DISPLAY_ORDER)
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        logger.error(f"手动清理市场数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}