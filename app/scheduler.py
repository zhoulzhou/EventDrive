import logging
import asyncio
from typing import List, Callable, Optional
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app import crud, schemas
from app.crawlers import (
    CLSDepthCrawler,
    EastmoneyDepthCrawler,
    NewsItem
)
from app.utils.image_downloader import download_image

logging.basicConfig(level=logging.INFO)
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
        return saved_count
        
    except Exception as e:
        log_crawl(f"❌ {crawler_class.__name__} 抓取出错: {str(e)}")
        logger.error(f"!!! {crawler_class.__name__} 抓取出错: {e}", exc_info=True)
        return 0
    finally:
        db.close()


async def full_crawl():
    log_crawl("=" * 50)
    log_crawl("🚀 开始执行完整新闻抓取任务...")
    log_crawl("=" * 50)
    start_time = datetime.now()
    
    crawlers = [
        CLSDepthCrawler,
        EastmoneyDepthCrawler
    ]
    
    log_crawl(f"将抓取 {len(crawlers)} 个新闻源")
    
    total_saved = 0
    for idx, crawler_class in enumerate(crawlers):
        log_crawl(f"--- 第 {idx+1}/{len(crawlers)} 个新闻源 ---")
        count = await crawl_single_source(crawler_class)
        total_saved += count
    
    duration = int((datetime.now() - start_time).total_seconds())
    log_crawl("=" * 50)
    log_crawl(f"✅ 抓取完成! 总共保存: {total_saved} 条, 耗时: {duration}秒")
    log_crawl("=" * 50)
    return total_saved


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            full_crawl,
            trigger=IntervalTrigger(hours=settings.CRAWL_INTERVAL_HOURS),
            id='full_crawl_job',
            name='Full news crawl',
            replace_existing=True
        )
        scheduler.start()
        logger.info(f"Scheduler started. Next crawl every {settings.CRAWL_INTERVAL_HOURS} hours.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
