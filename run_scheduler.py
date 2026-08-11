"""
独立调度器启动脚本 - 单独运行定时任务

启动方式:
    python run_scheduler.py

注意: 此脚本与 uvicorn (app.main:app) 分开运行。
    Web 服务只提供 API 和管理界面，不启动定时任务。
"""

import asyncio
import signal
import logging
from app.config import settings
from app.database import Base, engine
from app.scheduler import start_scheduler, stop_scheduler, scheduler
from app.utils.feishu_notifier import (
    init_all_notifiers, start_notifier, shutdown_notifier
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_shutdown_event = None


def _sync_create_tables():
    Base.metadata.create_all(bind=engine)


async def _main_async():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("收到停止信号，正在优雅关闭...")
        _shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    logger.info("初始化飞书推送器...")
    init_all_notifiers(
        nyt_url=settings.NYT_FEISHU_WEBHOOK_URL or "",
        nyt_keyword=settings.NYT_FEISHU_KEYWORD,
        bbc_url=settings.BBC_FEISHU_WEBHOOK_URL or "",
        bbc_keyword=settings.BBC_FEISHU_KEYWORD,
        dfcf_url=settings.DFCF_FEISHU_WEBHOOK_URL or "",
        dfcf_keyword=settings.DFCF_FEISHU_KEYWORD,
        index_url=settings.INDEX_FEISHU_WEBHOOK_URL or "",
        index_keyword=settings.INDEX_KEYWORD,
        kb_url=settings.KB_FEISHU_WEBHOOK_URL or "",
        kb_keyword=settings.KB_KEYWORD,
        openrouter_url=settings.OPENROUTER_FEISHU_WEBHOOK_URL or "",
        openrouter_keyword=settings.OPENROUTER_KEYWORD,
        deepseek_url=settings.DEEPSEEK_FEISHU_WEBHOOK_URL or "",
        deepseek_keyword=settings.DEEPSEEK_KEYWORD,
        x_url=settings.X_FEISHU_WEBHOOK_URL or "",
        x_keyword=settings.X_FEISHU_KEYWORD,
    )
    await start_notifier()
    logger.info("飞书推送器已启动")

    logger.info("启动APScheduler调度器...")
    start_scheduler()
    logger.info("APScheduler调度器已启动 (JST 8:00/12:00/16:00/20:00)")
    jobs = scheduler.get_jobs()
    for job in jobs:
        logger.info(f"  已注册任务: {job.name} (next: {job.next_run_time})")

    logger.info("调度器运行中，等待触发... (Ctrl+C 停止)")

    await _shutdown_event.wait()

    logger.info("正在关闭调度器...")
    stop_scheduler()
    logger.info("正在关闭飞书推送器...")
    await shutdown_notifier()
    logger.info("调度器已优雅退出")


if __name__ == "__main__":
    print("=" * 50)
    print("  EventDrive 新闻抓取调度器")
    print("=" * 50)
    print()

    _sync_create_tables()
    logger.info("数据库表已就绪")

    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        pass
    print("调度器已停止")
