import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app import crud
from app.crawlers.market_data import refresh_market_data
from app.market_strategy import compute_drawdown
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()

# 固定的卡片显示顺序，独立于抓取列表 SERIES；后续新增指数在此追加，不影响历史顺序
DISPLAY_ORDER = ["NASDAQ100", "VIXCLS", "DGS2", "DGS10"]


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
        "source": "FRED",
    }


@router.get("/market")
async def get_market_data(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """返回纳斯达克指数、VIX、美债2年期/10年期收益率的当前值（从数据库读取）。"""
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

        # 峰值回撤策略（数据在 market_strategy.json 配置、状态持久化在表）：按回撤幅度分级，附峰值信息
        for it in items:
            if it["value"] is None:
                it["drawdown_level"] = "normal"
                continue
            state = crud.get_market_strategy_state(db, it["symbol"])
            info = compute_drawdown(it["symbol"], it["value"], state, current_date=it["date"])
            it.update(info)

        return {
            "status": "ok",
            "items": items,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "FRED (St. Louis Fed)",
        }
    except Exception as e:
        logger.error(f"获取市场数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/market/history")
async def get_market_history(
    symbol: str = Query(..., description="FRED Series ID，如 NASDAQ100/VIXCLS/DGS2/DGS10"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回某个指数/收益率的历史序列（按日期升序），用于绘制曲线图。"""
    try:
        rows = crud.get_market_history(db, symbol)
        if not rows:
            return {"status": "ok", "symbol": symbol, "name": symbol, "unit": "", "points": []}
        return {
            "status": "ok",
            "symbol": symbol,
            "name": rows[0].name,
            "unit": rows[0].unit or "",
            "points": [{"date": r.date, "value": r.value} for r in rows],
        }
    except Exception as e:
        logger.error(f"获取历史数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}