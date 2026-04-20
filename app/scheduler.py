import logging
import asyncio
from typing import List, Callable, Optional, Dict
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import SessionLocal
from app import crud, schemas
from app.crawlers import (
    CLSDepthCrawler,
    EastmoneyDepthCrawler,
    NYTDepthCrawler,
    BBCCrawler,
    FinnhubIndexCrawler,
    NewsItem
)
from app.utils.image_downloader import download_image
from app.utils.feishu_notifier import dfcf_feishu_notify, cls_feishu_notify, nyt_feishu_notify, bbc_feishu_notify, doubao_feishu_notify, openrouter_feishu_notify, notify_index_alert, init_index_feishu_notifier
from app.utils.doubao_analyzer import init_doubao_analyzer, get_doubao_analyzer
from app.utils.openrouter_analyzer import init_openrouter_analyzer, get_openrouter_analyzer

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
                    "content": news_item.content,
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


async def crawl_indices():
    log_crawl("=" * 50)
    log_crawl("📊 开始执行指数监控任务...")
    log_crawl("=" * 50)

    crawler = None
    try:
        if settings.INDEX_FEISHU_WEBHOOK_URL:
            init_index_feishu_notifier(
                settings.INDEX_FEISHU_WEBHOOK_URL,
                "",
                settings.INDEX_KEYWORD
            )
            log_crawl("✅ 指数飞书推送已初始化")

        crawler = FinnhubIndexCrawler()
        alert_message = await crawler.crawl()

        if alert_message and settings.INDEX_FEISHU_WEBHOOK_URL:
            log_crawl("📤 正在发送指数监控通知...")
            result = await notify_index_alert(alert_message)
            log_crawl(f"📤 指数监控通知发送结果: {result}")
        elif alert_message:
            log_crawl(f"📊 指数监控结果:\n{alert_message}")
        else:
            log_crawl("⚠️ 未获取到指数数据")

        log_crawl("=" * 50)
        log_crawl("✅ 指数监控任务完成")
        log_crawl("=" * 50)

    except Exception as e:
        log_crawl(f"❌ 指数监控任务出错: {str(e)}")
        logger.error(f"!!! 指数监控任务出错: {e}", exc_info=True)
    finally:
        if crawler:
            crawler.close()


async def full_crawl():
    log_crawl("=" * 50)
    log_crawl("🚀 开始执行新闻抓取任务...")
    log_crawl("=" * 50)
    start_time = datetime.now()

    if settings.KB_API_KEY:
        try:
            init_doubao_analyzer(api_key=settings.KB_API_KEY, model=settings.KB_MODEL_ID, region=settings.KB_REGION)
            log_crawl("✅ 豆包大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"❌ 豆包分析器初始化失败: {e}", exc_info=True)

    if settings.OPENROUTER_API_KEY:
        try:
            init_openrouter_analyzer(api_key=settings.OPENROUTER_API_KEY)
            log_crawl("✅ OpenRouter大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"❌ OpenRouter分析器初始化失败: {e}", exc_info=True)

    doubao_analyzer = get_doubao_analyzer()
    openrouter_analyzer = get_openrouter_analyzer()

    total_saved = 0
    total_analyzed = 0

    log_crawl("=" * 50)
    log_crawl("📰 第1个新闻源: 东方财富")
    log_crawl("=" * 50)
    count, saved_news = await crawl_single_source(EastmoneyDepthCrawler)
    total_saved += count
    if saved_news:
        dfcf_feishu_notify(saved_news[:5], "东方财富")
        if doubao_analyzer:
            for news in saved_news[:2]:
                title = news.get('title', '')
                summary = news.get('summary', '')
                log_crawl(f"🔍 [豆包] 正在分析: {title[:50]}...")
                result = doubao_analyzer.analyze_only(title, summary, "东方财富")
                if result:
                    doubao_feishu_notify(title, result, "东方财富")
                    log_crawl(f"✅ [豆包] 分析并推送成功")
                else:
                    log_crawl(f"❌ [豆包] 分析失败")
                await asyncio.sleep(2)
    else:
        log_crawl("📭 东方财富没有新新闻")

    log_crawl("=" * 50)
    log_crawl("📰 第2个新闻源: 财联社")
    log_crawl("=" * 50)
    count, saved_news = await crawl_single_source(CLSDepthCrawler)
    total_saved += count
    if saved_news:
        cls_feishu_notify(saved_news[:5], "财联社")
    else:
        log_crawl("📭 财联社没有新新闻")

    log_crawl("=" * 50)
    log_crawl("📰 第3个新闻源: 纽约时报")
    log_crawl("=" * 50)
    count, saved_news = await crawl_single_source(NYTDepthCrawler)
    total_saved += count
    if saved_news:
        nyt_feishu_notify(saved_news[:5], "纽约时报")
        if openrouter_analyzer:
            for news in saved_news[:2]:
                title = news.get('title', '')
                summary = news.get('summary', '')
                log_crawl(f"🔍 [OpenRouter] 正在分析: {title[:50]}...")
                result = openrouter_analyzer.analyze_only(title, summary, "纽约时报")
                if result:
                    openrouter_feishu_notify(title, result, "纽约时报")
                    log_crawl(f"✅ [OpenRouter] 分析并推送成功")
                else:
                    log_crawl(f"❌ [OpenRouter] 分析失败")
                await asyncio.sleep(2)
    else:
        log_crawl("📭 纽约时报没有新新闻")

    log_crawl("=" * 50)
    log_crawl("📰 第4个新闻源: BBC")
    log_crawl("=" * 50)
    count, saved_news = await crawl_single_source(BBCCrawler)
    total_saved += count
    if saved_news:
        bbc_feishu_notify(saved_news[:5], "BBC")
        if openrouter_analyzer:
            for news in saved_news[:2]:
                title = news.get('title', '')
                summary = news.get('summary', '')
                log_crawl(f"🔍 [OpenRouter] 正在分析: {title[:50]}...")
                result = openrouter_analyzer.analyze_only(title, summary, "BBC")
                if result:
                    openrouter_feishu_notify(title, result, "BBC")
                    log_crawl(f"✅ [OpenRouter] 分析并推送成功")
                else:
                    log_crawl(f"❌ [OpenRouter] 分析失败")
                await asyncio.sleep(2)
    else:
        log_crawl("📭 BBC没有新新闻")

    log_crawl("=" * 50)
    log_crawl("📊 开始指数监控...")
    log_crawl("=" * 50)
    await crawl_indices()
    log_crawl("✅ Finnhub 指数监控完成")

    log_crawl("=" * 50)
    log_crawl(f"✅ 所有任务完成! 保存: {total_saved} 条, 耗时: {int((datetime.now() - start_time).total_seconds())}秒")
    log_crawl("=" * 50)
    log_crawl("=" * 50)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            full_crawl,
            trigger=CronTrigger(hour='0,3,6,9,12,15,18,21', minute=0),
            id='crawl_job_3h_intervals',
            name='Crawl at hours divisible by 3',
            replace_existing=True
        )
        scheduler.add_job(
            crawl_indices,
            trigger=CronTrigger(hour='*', minute=0),
            id='index_crawl_job_1h_intervals',
            name='Crawl indices every hour',
            replace_existing=True
        )
        scheduler.start()
        logger.info("Scheduler started. Crawl at 0,3,6,9,12,15,18,21 hours. Index crawl every hour.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped.")