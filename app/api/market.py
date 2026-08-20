import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app import crud
from app.crawlers.market_data import refresh_market_data
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()

# 固定的卡片显示顺序，独立于抓取列表 SERIES；后续新增指数在此追加，不影响历史顺序
DISPLAY_ORDER = ["NASDAQ100", "VIXCLS", "DGS2", "DGS10"]

# 纳斯达克100峰值回撤预警：以 2026-08-14 的 30050 为当前峰值基准
NASDAQ100_PEAK = {"value": 30050.0, "date": "2026-08-14"}
DRAWDOWN_THRESHOLD = 0.10  # 当前值较峰值回撤 >= 10% 时卡片变红


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

        # 纳斯达克100峰值回撤预警：附带峰值信息，供前端显示峰值与回撤百分比、触发红色预警
        for it in items:
            if it["symbol"] == "NASDAQ100" and it["value"] is not None:
                peak = NASDAQ100_PEAK["value"]
                it["peak_value"] = peak
                it["peak_date"] = NASDAQ100_PEAK["date"]
                it["peak_change_percent"] = round((it["value"] - peak) / peak * 100, 2)
                it["drawdown_alarm"] = it["value"] <= peak * (1 - DRAWDOWN_THRESHOLD)
            else:
                it["drawdown_alarm"] = False

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