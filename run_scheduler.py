#!/usr/bin/env python3
import asyncio
import sys
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config import settings
from app.database import engine, Base
from app.utils.feishu_notifier import init_feishu_notifier, init_nyt_feishu_notifier, init_bbc_feishu_notifier, init_em_feishu_notifier
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

    if settings.FEISHU_WEBHOOK_URL and settings.FEISHU_SECRET:
        init_feishu_notifier(
            settings.FEISHU_WEBHOOK_URL,
            settings.FEISHU_SECRET,
            settings.FEISHU_KEYWORD
        )
        print("✅ 飞书推送已初始化")
    else:
        print("⚠️ 飞书推送未配置")

    if settings.NYT_FEISHU_WEBHOOK_URL:
        init_nyt_feishu_notifier(
            settings.NYT_FEISHU_WEBHOOK_URL,
            "",
            settings.NYT_FEISHU_KEYWORD
        )
        print("✅ 纽约时报飞书推送已初始化")
    else:
        print("⚠️ 纽约时报飞书推送未配置")

    if settings.BBC_FEISHU_WEBHOOK_URL:
        init_bbc_feishu_notifier(
            settings.BBC_FEISHU_WEBHOOK_URL,
            "",
            settings.BBC_FEISHU_KEYWORD
        )
        print("✅ BBC飞书推送已初始化")
    else:
        print("⚠️ BBC飞书推送未配置")

    if settings.EM_FEISHU_WEBHOOK_URL:
        init_em_feishu_notifier(
            settings.EM_FEISHU_WEBHOOK_URL,
            "",
            settings.EM_FEISHU_KEYWORD
        )
        print("✅ 东方财富飞书推送已初始化")
    else:
        print("⚠️ 东方财富飞书推送未配置")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_scheduler()
    print("✅ 定时任务调度器已启动")
    print("📰 服务运行中，每3小时自动抓取并推送...")
    print("按 Ctrl+C 停止")

    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        print("\n🛑 正在关闭...")
        stop_scheduler()

if __name__ == "__main__":
    main()