import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas
from app.scheduler import full_crawl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

crawl_status = {"is_running": False, "last_run": None}


@router.post("/crawl/trigger")
async def trigger_crawl(background_tasks: BackgroundTasks):
    logger.info("📢 收到手动抓取请求")
    
    if crawl_status["is_running"]:
        logger.warning("抓取任务已在进行中，拒绝新请求")
        return {"status": "already_running", "message": "Crawl is already in progress"}
    
    logger.info("✅ 开始启动抓取任务...")
    crawl_status["is_running"] = True
    
    async def run_crawl():
        try:
            logger.info("🕐 抓取任务开始执行...")
            await full_crawl()
            logger.info("✨ 抓取任务执行完毕")
        except Exception as e:
            logger.error(f"❌ 抓取任务执行出错: {e}", exc_info=True)
        finally:
            crawl_status["is_running"] = False
            import datetime
            crawl_status["last_run"] = datetime.datetime.now().isoformat()
            logger.info(f"📅 最后抓取时间: {crawl_status['last_run']}")
    
    background_tasks.add_task(lambda: asyncio.create_task(run_crawl()))
    
    logger.info("🚀 抓取任务已加入后台队列")
    return {"status": "started", "message": "Crawl has been triggered"}


@router.get("/crawl/status")
def get_crawl_status(db: Session = Depends(get_db)):
    latest_log = crud.get_latest_crawl_log(db)
    
    return {
        "is_running": crawl_status["is_running"],
        "last_run": crawl_status["last_run"],
        "latest_log": schemas.CrawlLog.model_validate(latest_log) if latest_log else None
    }
