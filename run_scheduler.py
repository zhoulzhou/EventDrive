#!/usr/bin/env python3
import asyncio
import sys
import signal
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from app.config import settings
from app.database import engine, Base
from app.utils.feishu_notifier import init_all_notifiers
from app.scheduler import start_scheduler, stop_scheduler, full_crawl

def signal_handler(signum, frame):
    print("\n🛑 收到停止信号，正在关闭...")
    stop_scheduler()
    sys.exit(0)

def main():
    print("=" * 60)
    print("🚀 纽约时报新闻抓取服务正在启动...")
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
    now = datetime.now()
    print("✅ 定时任务调度器已启动")
    print(f"🕐 服务器当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (24小时制)")
    print("📰 服务运行中，每日 8:00 / 12:00 / 16:00 / 20:00 自动抓取并推送（服务器本地时间）...")
    print("按 Ctrl+C 停止")

    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        print("\n🛑 正在关闭...")
        stop_scheduler()

if __name__ == "__main__":
    main()