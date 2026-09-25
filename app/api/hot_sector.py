"""今日热点接口：A 股概念板块热点前三 + 驱动原因 + 板块内前五个股交易情况。

- GET /api/hot-sector            读取今日热点（10 分钟内复用内存缓存）
- GET /api/hot-sector?refresh=1  强制重新抓取（页面「刷新」按钮）

数据抓取走 akshare（东方财富公开接口）。热点盘中变化快，页面打开时实时抓取、
10 分钟内复用缓存，具体口径与缓存见 app/utils/hot_sector.py。

每次实际抓取（未命中缓存）的结果按 (交易日, 板块) 写入 hot_sector_snapshots 表供后续复盘；
该「抓取 → 落库」逻辑与定时任务共用 app/utils/hot_sector_refresh.py，
定时任务在收盘后（北京时间 20:35）补抓一次，保证不开页面也能留下每日复盘记录。
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.api.login import require_auth
from app.utils.hot_sector_refresh import fetch_and_store_hot_sector

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/hot-sector")
async def get_hot_sector(
    refresh: bool = Query(False, description="为 true 时绕过缓存强制重新抓取"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回今日热点板块（概念板块·涨幅为主+资金为辅综合前三）及驱动原因、板块前五个股。"""
    try:
        return await fetch_and_store_hot_sector(force=refresh, db=db)
    except Exception as e:
        logger.error("获取今日热点失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}