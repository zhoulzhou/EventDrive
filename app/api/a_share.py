"""股市行情（A 股宽基指数）：当前点位 + 今年以来曲线。

只读库 + 一次手动刷新入口。数据由收盘轮（北京时间 21:34，域 `a_share`）抓取落库，
本接口自己不抓 —— 除了库里一片空白时会兜底抓一次（首次部署/新环境）。

落库表沿用 `market_prices`（symbol = SH000001 / SH000688），
与「全球行情」页共用一张表但互不干扰：那边按 DISPLAY_ORDER 显式过滤美股四项。
"""
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import crud
from app.api.login import require_auth
from app.database import get_db
from app.utils.index_daily import INDEX_SERIES, SYMBOLS, UNIT, ytd_start

logger = logging.getLogger(__name__)

router = APIRouter()

SOURCE_HINT = "腾讯日线（新浪指数日线 / 东财日线兜底）"


def _build_item(meta: Dict[str, str], rows: List[Any]) -> Dict[str, Any]:
    """把库里的行整理成「最新点位 + 涨跌 + 年内涨跌 + 高低点 + 曲线点」。"""
    points = [{"date": str(r.date)[:10], "value": r.value}
              for r in rows if r.value is not None]
    item: Dict[str, Any] = {
        "symbol": meta["symbol"],
        "name": meta["name"],
        "short": meta.get("short") or meta["name"],
        "code": meta["symbol"].replace("SH", "") + ".SH",
        "unit": UNIT,
        "value": None, "date": None,
        "change": None, "change_percent": None,
        "ytd_percent": None, "ytd_base": None,
        "high": None, "low": None,
        "stale_days": None,
        "points": points,
    }
    if not points:
        return item

    last = points[-1]
    item["value"] = last["value"]
    item["date"] = last["date"]
    item["points"] = points

    if len(points) >= 2:
        prev = points[-2]
        if prev["value"]:
            item["change"] = round(last["value"] - prev["value"], 2)
            item["change_percent"] = round(
                (last["value"] - prev["value"]) / prev["value"] * 100, 2)

    # 年内涨跌幅：以今年第一个交易日收盘为基准（库里首行就是今年首个交易日）
    base = points[0]
    item["ytd_base"] = {"date": base["date"], "value": base["value"]}
    if base["value"]:
        item["ytd_percent"] = round((last["value"] - base["value"]) / base["value"] * 100, 2)

    hi = max(points, key=lambda p: p["value"])
    lo = min(points, key=lambda p: p["value"])
    item["high"] = {"date": hi["date"], "value": hi["value"]}
    item["low"] = {"date": lo["date"], "value": lo["value"]}

    try:
        item["stale_days"] = (date.today() - date.fromisoformat(last["date"])).days
    except ValueError:
        item["stale_days"] = None
    return item


@router.get("/a-share")
def get_a_share(
    since: Optional[str] = Query(None, description="起始日期，默认今年 1 月 1 日"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回上证指数与科创50 的最新点位、涨跌与今年以来的曲线点（按日期升序）。

    一次批量读两个 symbol（云库下省一次往返），缺数据时返回 points=[]，
    由页面提示「尚未回填」而不是报错。
    """
    start = since or ytd_start()
    try:
        rows_map = crud.get_market_history_multi(db, list(SYMBOLS), since=start)
        items = [_build_item(meta, rows_map.get(meta["symbol"]) or [])
                 for meta in INDEX_SERIES]
        has_data = any(it["points"] for it in items)
        return {
            "status": "ok",
            "items": items,
            "since": start,
            "today": date.today().isoformat(),
            "has_data": has_data,
            "source": SOURCE_HINT,
            "updated_at": date.today().isoformat(),
        }
    except Exception as e:  # noqa: BLE001
        logger.error("获取 A 股指数行情失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/a-share/refresh")
def refresh_a_share(
    since: Optional[str] = Query(None, description="起始日期，默认今年 1 月 1 日"),
    force: bool = Query(False, description="true=把区间内所有点整体重写（默认只补缺口+重写当天）"),
    auth: bool = Depends(require_auth),
):
    """手动抓取/回填（同步执行，约 2~6 秒）。

    首次部署、或定时任务连挂几天后，用这个接口把今年以来的历史一次补齐。
    日常不需要点：收盘轮会自动补缺口。
    """
    try:
        from app.utils.index_daily import refresh_a_share_index
        data = refresh_a_share_index(since=since, force=force)
        return {
            "status": "ok",
            "since": data["since"],
            "source": data["source"],
            "summary": data["summary"],
            "items": [
                {k: it.get(k) for k in
                 ("symbol", "name", "ok", "source_label", "fetched", "added", "latest", "error")}
                for it in data["items"]
            ],
        }
    except Exception as e:  # noqa: BLE001
        logger.error("手动刷新 A 股指数失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
