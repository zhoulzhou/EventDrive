"""今日热点板块快照的抓取与落库（供 API 与定时任务共用）。

把「抓取 → 落库」从接口层抽出来，让页面接口与后台定时任务用同一份实现，
避免两处逻辑漂移：

- fetch_and_store_hot_sector()：抓取概念板块热点前三（含驱动原因、板块内前五个股），
  按（交易日, 板块代码）写入 hot_sector_snapshots，供后续复盘。

定时任务在 app/scheduler.py 注册（北京时间 20:35，避让 20:30 的市场/宏观/股票三件套），
由独立进程 run_scheduler.py 执行。抓取本身是异步的（akshare 阻塞调用已在线程池内执行），
因此这里保持 async，定时任务直接 await 即可，不需要再套 to_thread。
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import hot_sector as hs

logger = logging.getLogger(__name__)


async def fetch_and_store_hot_sector(
    force: bool = True, db: Optional[Session] = None
) -> Dict[str, Any]:
    """抓取今日热点并按 (交易日, 板块) 落库，返回与 /api/hot-sector 同构的 payload。

    - db 传入时复用调用方的会话（Web 接口），不传则自建并负责关闭（定时任务）。
    - 命中缓存时不重复落库（上次抓取时已写过）。
    - 落库失败只追加 errors 提示，不影响返回的热点数据。
    """
    own_session = db is None
    session = db if db is not None else SessionLocal()
    try:
        news_titles = crud.get_recent_news_titles(session, hours=48, limit=40)
        payload = await hs.build_hot_sector_payload(news_titles=news_titles, force=force)

        if payload.get("status") == "ok" and not payload.get("cached"):
            try:
                payload["stored_sectors"] = crud.save_hot_sector_snapshots(
                    session,
                    trade_date=payload["trade_date"],
                    sectors=payload["sectors"],
                    reason_source=payload.get("reason_source"),
                    reason_model=payload.get("reason_model"),
                )
            except Exception as e:
                logger.error("今日热点快照落库失败: %s", e, exc_info=True)
                payload.setdefault("errors", []).append("快照落库失败，本次数据未存档")

        return payload
    finally:
        if own_session:
            session.close()