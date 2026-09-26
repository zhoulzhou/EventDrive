"""今日热点板块快照的抓取与落库（供 API 与定时任务共用）。

把「抓取 → 落库」从接口层抽出来，让页面接口与后台定时任务用同一份实现，
避免两处逻辑漂移：

- fetch_and_store_hot_sector()：抓取概念板块热点前三（含驱动原因、板块内前五个股），
  按（交易日, 板块代码）写入 hot_sector_snapshots，供页面只读展示与后续复盘。

两个调用方：
- Web 接口 POST /api/hot-sector/refresh（页面「刷新盘面」按钮），复用请求的 db 会话；
- 收盘后定时任务（北京时间 20:35，见 app/scheduler.py），保证不开页面也能留下每日记录。

定时任务由独立进程 run_scheduler.py 执行。抓取本身是异步的（akshare 阻塞调用已在线程池内
执行），因此这里保持 async，定时任务直接 await 即可，不需要再套 to_thread。
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import crud
from app.database import SessionLocal
from app.utils import hot_sector as hs

logger = logging.getLogger(__name__)


async def fetch_and_store_hot_sector(db: Optional[Session] = None) -> Dict[str, Any]:
    """抓取今日热点并按 (交易日, 板块) 落库，返回抓取结果。

    - db 传入时复用调用方的会话（Web 接口），不传则自建并负责关闭（定时任务）。
    - 每次调用都真实抓取（不做缓存），因此只要 status=ok 就一定落库。
    - 落库失败只追加 errors 提示，不影响返回的热点数据。
    """
    own_session = db is None
    session = db if db is not None else SessionLocal()
    try:
        payload = await hs.build_hot_sector_payload()

        if payload.get("status") == "ok":
            try:
                payload["stored_sectors"] = crud.save_hot_sector_snapshots(
                    session,
                    trade_date=payload["trade_date"],
                    sectors=payload["sectors"],
                    reason_source="rule",
                )
            except Exception as e:
                logger.error("今日热点快照落库失败: %s", e, exc_info=True)
                payload.setdefault("errors", []).append("快照落库失败，本次数据未存档")

        return payload
    finally:
        if own_session:
            session.close()