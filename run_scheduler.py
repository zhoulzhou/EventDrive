#!/usr/bin/env python3
import asyncio
import sys
import signal
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from app.config import settings
from app.database import engine, Base
from app.utils.feishu_notifier import init_all_notifiers, start_notifier, shutdown_notifier
from app.scheduler import start_scheduler, stop_scheduler, full_crawl

def signal_handler(signum, frame):
    print("\n🛑 收到停止信号，正在关闭...")
    stop_scheduler()
    sys.exit(0)

def main():
    print("=" * 60)
    print("🚀 EventDrive 新闻抓取调度器正在启动...")
    print("=" * 60)

    Base.metadata.create_all(bind=engine)
    print("✅ 数据库表初始化完成")

    init_all_notifiers(
        nyt_url=settings.NYT_FEISHU_WEBHOOK_URL or "",
        nyt_keyword=settings.NYT_FEISHU_KEYWORD,
        bbc_url=settings.BBC_FEISHU_WEBHOOK_URL or "",
        bbc_keyword=settings.BBC_FEISHU_KEYWORD,
        dfcf_url=settings.DFCF_FEISHU_WEBHOOK_URL or "",
        dfcf_keyword=settings.DFCF_FEISHU_KEYWORD,
        cls_url=settings.CLS_FEISHU_WEBHOOK_URL or "",
        cls_keyword=settings.CLS_FEISHU_KEYWORD,
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
    print("✅ 飞书推送已初始化")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_scheduler()
    loop = asyncio.get_event_loop()
    loop.create_task(start_notifier())
    tokyo_tz = ZoneInfo("Asia/Tokyo")
    now_tokyo = datetime.now(tokyo_tz)
    now_local = datetime.now()
    print("✅ 定时任务调度器已启动")
    print(f"🕐 服务器本地时间: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🗼 日本时间(Asia/Tokyo): {now_tokyo.strftime('%Y-%m-%d %H:%M:%S')}")
    print("📰 服务运行中，每日 8:00 / 12:00 / 16:00 / 20:00（日本时间）自动抓取...")
    print("按 Ctrl+C 停止")

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n🛑 正在关闭...")
        stop_scheduler()
        loop.run_until_complete(shutdown_notifier())

if __name__ == "__main__":
    main()