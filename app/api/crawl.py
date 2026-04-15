import asyncio
import logging
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas
from app.scheduler import full_crawl, set_crawl_progress_callback
from app.api.login import require_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

crawl_status: Dict[str, any] = {
    "is_running": False,
    "last_run": None,
    "current_source": None,
    "current_step": None,
    "steps": [],
    "logs": []
}
crawl_task = None


def crawl_log_handler(message: str):
    import datetime
    crawl_status["logs"].append({
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "message": message
    })
    if len(crawl_status["logs"]) > 50:
        crawl_status["logs"] = crawl_status["logs"][-50:]


@router.post("/crawl/trigger")
async def trigger_crawl():
    global crawl_task
    logger.info("📢 收到手动抓取请求")

    if crawl_status["is_running"]:
        logger.warning("抓取任务已在进行中，拒绝新请求")
        return {"status": "already_running", "message": "Crawl is already in progress"}

    crawl_status["is_running"] = True
    crawl_status["current_source"] = None
    crawl_status["current_step"] = "正在初始化..."
    crawl_status["steps"] = []
    crawl_status["logs"] = []
    crawl_log_handler("🚀 开始抓取任务...")

    set_crawl_progress_callback(crawl_log_handler)

    async def run_crawl():
        try:
            crawl_log_handler("🕐 抓取任务开始执行...")
            crawl_status["current_step"] = "正在抓取各新闻源..."
            await full_crawl()
            crawl_log_handler("✨ 抓取任务执行完毕")
            crawl_status["current_step"] = "抓取完成"
        except Exception as e:
            crawl_log_handler(f"❌ 抓取出错: {str(e)}")
            logger.error(f"❌ 抓取任务执行出错: {e}", exc_info=True)
        finally:
            crawl_status["is_running"] = False
            crawl_status["current_source"] = None
            import datetime
            crawl_status["last_run"] = datetime.datetime.now().isoformat()
            logger.info(f"📅 最后抓取时间: {crawl_status['last_run']}")

    crawl_task = asyncio.create_task(run_crawl())

    logger.info("🚀 抓取任务已启动")
    crawl_log_handler("🚀 抓取任务已启动")
    return {"status": "started", "message": "Crawl has been triggered"}


@router.get("/crawl/status")
def get_crawl_status(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    latest_log = crud.get_latest_crawl_log(db)

    response = {
        "is_running": crawl_status["is_running"],
        "last_run": crawl_status["last_run"],
        "current_source": crawl_status.get("current_source"),
        "current_step": crawl_status.get("current_step"),
        "steps": crawl_status.get("steps", []),
        "logs": crawl_status.get("logs", []),
        "latest_log": schemas.CrawlLog.model_validate(latest_log) if latest_log else None
    }
    return response
