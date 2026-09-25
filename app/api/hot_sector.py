"""今日热点接口：A 股概念板块热点前三 + 驱动原因 + 板块内前五个股交易情况。

- GET /api/hot-sector            读取今日热点（10 分钟内复用内存缓存）
- GET /api/hot-sector?refresh=1  强制重新抓取（页面「刷新」按钮）

数据抓取走 akshare（东方财富公开接口），**不做定时落库**：热点盘中变化快，
页面实时抓取更贴合"今日"语义，缓存与强制刷新见 app/utils/hot_sector.py。

每次实际抓取（未命中缓存）的结果按 (交易日, 板块) 写入 hot_sector_snapshots 表，
供后续复盘；命中缓存时不重复写库。
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth
from app.utils import hot_sector as hs

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
        # 近期新闻标题作为大模型归因的素材；库中无新闻时传空列表即可
        news_titles = crud.get_recent_news_titles(db, hours=48, limit=40)
        payload = await hs.build_hot_sector_payload(news_titles=news_titles, force=refresh)

        # 只在本次真的抓取了数据时落库（命中缓存的数据上次已写过），失败不影响页面展示
        if payload.get("status") == "ok" and not payload.get("cached"):
            try:
                crud.save_hot_sector_snapshots(
                    db,
                    trade_date=payload["trade_date"],
                    sectors=payload["sectors"],
                    reason_source=payload.get("reason_source"),
                    reason_model=payload.get("reason_model"),
                )
            except Exception as e:
                logger.error("今日热点快照落库失败: %s", e, exc_info=True)
                payload.setdefault("errors", []).append("快照落库失败，本次数据未存档")

        return payload
    except Exception as e:
        logger.error("获取今日热点失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}