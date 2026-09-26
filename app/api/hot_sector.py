"""今日热点接口：A 股概念板块热点前三 + 驱动原因 + 板块内前五个股交易情况。

- GET  /api/hot-sector          读取库中已有热点快照（**只读库，不触发抓取**）
- POST /api/hot-sector/refresh  强制抓取今日热点并落库（页面「刷新盘面」按钮走这里）

打开页面只读库、不发起任何外部请求，所以是瞬时的；要立刻更新就点页面的「刷新盘面」按钮。
抓取实现见 app/utils/hot_sector.py，落库按 (交易日, 板块代码) 覆盖写入 hot_sector_snapshots，
「抓取 → 落库」逻辑与收盘后定时任务共用 app/utils/hot_sector_refresh.py。

数据有两个写入时机，页面读到的就是其中之一：
1. 用户点「刷新盘面」（POST /refresh）；
2. 收盘后定时任务（北京时间 20:35，见 app/scheduler.py），保证不开页面也能留下每日复盘记录。
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils.hot_sector_refresh import fetch_and_store_hot_sector

logger = logging.getLogger(__name__)

router = APIRouter()

# 库中时间戳为 UTC（见库表 server_default=func.now()），展示时转北京时间
CN_TZ = ZoneInfo("Asia/Shanghai")


def _local_str(ts: Optional[datetime]) -> str:
    if ts is None:
        return "—"
    return ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _load_stocks(raw: Optional[str]) -> list:
    """把库中 stocks 字段（JSON 字符串）还原成个股列表，脏数据一律当空列表处理。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("热点快照 stocks 字段解析失败，按空列表处理")
        return []
    return data if isinstance(data, list) else []


def _sector_from_row(row) -> dict:
    return {
        "rank": row.board_rank,
        "name": row.board_name,
        "code": row.board_code,
        "change_percent": row.change_percent,
        "turnover_rate": row.turnover_rate,
        "up_count": row.up_count,
        "down_count": row.down_count,
        "lead_stock": row.lead_stock,
        "lead_stock_change": row.lead_stock_change,
        "reason": row.reason,
        "stocks": _load_stocks(row.stocks),
    }


def _payload_from_db(db: Session) -> dict:
    """读取库中最新交易日的热点快照，组装成页面所需结构（不发外部请求）。"""
    trade_date, rows, fetched_at = crud.get_latest_hot_sector_snapshot(db)
    if not rows:
        return {
            "status": "empty",
            "message": "库中暂无热点快照，点击「刷新盘面」抓取当前盘面数据。",
            "sectors": [],
        }

    return {
        "status": "ok",
        "trade_date": trade_date,
        "generated_at": _local_str(fetched_at),
        "source": "新浪财经 · akshare 实时行情（库中快照）",
        "sectors": [_sector_from_row(row) for row in rows],
        "errors": [],
    }


@router.get("/hot-sector")
def get_hot_sector(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """读取库中已有的热点快照（**只读**，不触发抓取），页面打开时调用。"""
    try:
        return _payload_from_db(db)
    except Exception as e:
        logger.error("读取今日热点失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/hot-sector/refresh")
async def refresh_hot_sector(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """强制抓取今日热点并落库（页面「刷新盘面」按钮，与定时任务走同一实现）。

    抓取成功后回读库中数据返回，保证与打开页面时的响应结构完全一致；
    抓取失败（数据源异常）则把失败原因原样返回，不改动库中已有快照。
    """
    try:
        fetched = await fetch_and_store_hot_sector(db=db)
        if fetched.get("status") != "ok":
            return fetched

        payload = _payload_from_db(db)
        payload["errors"] = fetched.get("errors") or []
        return payload
    except Exception as e:
        logger.error("刷新今日热点失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}