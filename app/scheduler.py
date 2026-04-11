import logging
import asyncio
from typing import List, Callable, Optional, Dict
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app import crud, schemas
from app.crawlers import (
    CLSDepthCrawler,
    EastmoneyDepthCrawler,
    NYTDepthCrawler,
    NewsItem
)
from app.utils.image_downloader import download_image
from app.utils.feishu_notifier import notify_new_news, notify_nyt_news, notify_no_news, init_feishu_notifier, init_nyt_feishu_notifier

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

crawl_progress_callback: Optional[Callable] = None


def set_crawl_progress_callback(callback: Callable):
    global crawl_progress_callback
    crawl_progress_callback = callback


def log_crawl(message: str):
    logger.info(message)
    if crawl_progress_callback:
        crawl_progress_callback(message)


async def process_news_item(news_item: NewsItem):
    image_path = None
    if news_item.image_url:
        try:
            image_path = await download_image(news_item.image_url)
        except Exception as e:
            logger.warning(f"Failed to download image for {news_item.url}: {e}")

    return schemas.NewsCreate(
        title=news_item.title,
        content=news_item.content,
        source=news_item.source,
        publish_time=news_item.publish_time,
        url=news_item.url,
        author=news_item.author,
        summary=news_item.summary,
        image_path=image_path
    )


async def crawl_single_source(crawler_class):
    db = SessionLocal()
    saved_news = []
    try:
        crawler = crawler_class()
        source_name = crawler.source_name
        log_crawl(f"📰 开始抓取: {source_name}")

        news_items = await crawler.crawl()
        log_crawl(f"[{source_name}] 获取到 {len(news_items)} 条新闻，准备保存...")

        saved_count = 0

        for idx, news_item in enumerate(news_items):
            log_crawl(f"[{source_name}] 处理第 {idx+1}/{len(news_items)} 条: {news_item.title[:30]}...")
            if not crud.is_news_exists(db, news_item.url):
                news_create = await process_news_item(news_item)
                crud.create_news(db, news_create)
                saved_count += 1
                saved_news.append({
                    "title": news_item.title,
                    "url": news_item.url,
                    "publish_time": news_item.publish_time.isoformat() if news_item.publish_time else "",
                    "source": news_item.source,
                    "summary": news_item.summary,
                    "news_type": getattr(news_item, 'news_type', None)
                })
                log_crawl(f"[{source_name}] ✅ 保存成功 (累计: {saved_count})")
            else:
                log_crawl(f"[{source_name}] ⏭️ 已存在，跳过")

        log = schemas.CrawlLogCreate(
            source=crawler.source_name,
            news_count=saved_count,
            status=crawler.get_status(),
            error_message=crawler.error_message,
            duration=crawler.get_crawl_duration()
        )
        crud.create_crawl_log(db, log)

        log_crawl(f"🏁 {source_name} 抓取完成: 保存 {saved_count} 条")
        return saved_count, saved_news

    except Exception as e:
        log_crawl(f"❌ {crawler_class.__name__} 抓取出错: {str(e)}")
        logger.error(f"!!! {crawler_class.__name__} 抓取出错: {e}", exc_info=True)
        return 0, []
    finally:
        db.close()


async def full_crawl():
    log_crawl("=" * 50)
    log_crawl("🚀 开始执行新闻抓取任务...")
    log_crawl("=" * 50)
    start_time = datetime.now()

    crawlers = [
        CLSDepthCrawler,
        EastmoneyDepthCrawler,
        NYTDepthCrawler
    ]

    log_crawl(f"将抓取 {len(crawlers)} 个新闻源")

    total_saved = 0
    all_saved_news = []

    for idx, crawler_class in enumerate(crawlers):
        log_crawl(f"--- 第 {idx+1}/{len(crawlers)} 个新闻源 ---")
        count, saved_news = await crawl_single_source(crawler_class)
        total_saved += count
        all_saved_news.extend(saved_news)

    duration = int((datetime.now() - start_time).total_seconds())
    log_crawl("=" * 50)
    log_crawl(f"✅ 抓取完成! 总共保存: {total_saved} 条, 耗时: {duration}秒")
    log_crawl("=" * 50)

    if settings.FEISHU_WEBHOOK_URL:
        if all_saved_news:
            news_by_source = {}
            for news in all_saved_news:
                source = news.get('source', '未知来源')
                news_type = news.get('news_type')

                if news_type in ('wire', 'topstories'):
                    key = f"{source}_{news_type}"
                else:
                    key = source

                if key not in news_by_source:
                    news_by_source[key] = []
                news_by_source[key].append(news)

            for key, news_list in news_by_source.items():
                if news_list:
                    if 'wire' in key:
                        display_source = "纽约时报最新资讯"
                        notify_func = notify_nyt_news
                    elif 'topstories' in key:
                        display_source = "纽约时报精选"
                        notify_func = notify_nyt_news
                    else:
                        display_source = key
                        notify_func = notify_new_news

                    log_crawl(f"📤 正在发送 {display_source} 飞书通知...")
                    try:
                        result = await notify_func(news_list[:5], display_source)
                        log_crawl(f"📤 {display_source} 飞书通知发送结果: {result}")
                    except Exception as e:
                        logger.error(f"{display_source} 飞书通知发送失败: {e}", exc_info=True)
        else:
            log_crawl("📤 没有新新闻，发送无新新闻通知...")
            try:
                await notify_no_news()
                log_crawl("📤 无新新闻通知已发送")
            except Exception as e:
                logger.error(f"无新新闻通知发送失败: {e}", exc_info=True)
    else:
        if not all_saved_news:
            logger.info("飞书通知跳过: 没有新保存的新闻")
        if not settings.FEISHU_WEBHOOK_URL:
            logger.info("飞书通知跳过: FEISHU_WEBHOOK_URL 未配置")

    return total_saved


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            full_crawl,
            trigger=CronTrigger(hour=12, minute=0),
            id='crawl_job_12',
            name='Crawl at 12:00',
            replace_existing=True
        )
        scheduler.add_job(
            full_crawl,
            trigger=IntervalTrigger(hours=3, start_date=datetime.now()),
            id='crawl_job_3h',
            name='Crawl every 3 hours',
            replace_existing=True
        )
        scheduler.start()
        logger.info("Scheduler started. Crawl every 3 hours starting from 12:00.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped.")