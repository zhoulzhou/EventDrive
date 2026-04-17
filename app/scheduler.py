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
from app.utils.feishu_notifier import notify_new_news, notify_nyt_news, notify_bbc_news, notify_em_news, notify_no_news, notify_index_alert, init_feishu_notifier, init_nyt_feishu_notifier, init_bbc_feishu_notifier, init_em_feishu_notifier, init_index_feishu_notifier, send_analysis_to_feishu
from app.utils.doubao_analyzer import init_doubao_analyzer, get_doubao_analyzer
from app.utils.knowledge_analyzer import init_knowledge_analyzer, get_knowledge_analyzer

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

    crawlers = [
        ("东方财富", EastmoneyDepthCrawler, notify_em_news, "豆包"),
        ("财联社", CLSDepthCrawler, notify_new_news, "OpenRouter"),
        ("纽约时报", NYTDepthCrawler, notify_nyt_news, "豆包"),
        ("BBC", BBCCrawler, notify_bbc_news, "OpenRouter"),
    ]

    if settings.KB_API_KEY:
        try:
            init_doubao_analyzer(
                api_key=settings.KB_API_KEY,
                model=settings.KB_MODEL_ID,
                region=settings.KB_REGION
            )
            log_crawl("✅ 豆包大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"❌ 豆包分析器初始化失败: {e}", exc_info=True)

    if settings.OPENROUTER_API_KEY:
        try:
            init_knowledge_analyzer(
                api_key=settings.OPENROUTER_API_KEY
            )
            log_crawl("✅ OpenRouter大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"❌ OpenRouter分析器初始化失败: {e}", exc_info=True)

    doubao_analyzer = get_doubao_analyzer()
    openrouter_analyzer = get_knowledge_analyzer()

    log_crawl(f"📊 豆包分析器状态: {doubao_analyzer is not None}")
    log_crawl(f"📊 OpenRouter分析器状态: {openrouter_analyzer is not None}")

    total_saved = 0
    total_analyzed = 0

    for idx, (source_name, crawler_class, notify_func, model_name) in enumerate(crawlers):
        log_crawl(f"--- 第 {idx+1}/{len(crawlers)} 个新闻源: {source_name} ---")
        count, saved_news = await crawl_single_source(crawler_class)
        total_saved += count

        if not saved_news:
            log_crawl(f"📭 {source_name} 没有新新闻")
            continue

        log_crawl(f"📤 正在发送 {source_name} 飞书通知...")
        try:
            result = await notify_func(saved_news[:5], source_name)
            log_crawl(f"📤 {source_name} 飞书通知发送结果: {result}")
        except Exception as e:
            logger.error(f"{source_name} 飞书通知发送失败: {e}", exc_info=True)

        analyzer = doubao_analyzer if model_name == "豆包" else openrouter_analyzer
        keyword = settings.KB_KEYWORD if model_name == "豆包" else settings.OPENROUTER_KEYWORD

        if not analyzer:
            log_crawl(f"⚠️ {model_name}分析器未初始化，跳过分析")
        else:
            news_to_analyze = saved_news[:2]
            for n_idx, news in enumerate(news_to_analyze, 1):
                news_title = news.get('title', '')
                news_content = news.get('content', news.get('summary', ''))

                log_crawl(f"🔍 [{model_name}] 正在分析 {source_name} 第 {n_idx}/{len(news_to_analyze)} 条: {news_title[:50]}...")

                try:
                    analysis_result = analyzer.analyze_only(
                        news_title=news_title,
                        news_content=news_content,
                        source=source_name
                    )
                    if analysis_result:
                        push_ok = send_analysis_to_feishu(news_title, analysis_result, source_name, keyword)
                        if push_ok:
                            log_crawl(f"✅ [{model_name}] {source_name} 第 {n_idx} 条分析并推送成功")
                            total_analyzed += 1
                        else:
                            log_crawl(f"❌ [{model_name}] {source_name} 第 {n_idx} 条推送失败")
                    else:
                        log_crawl(f"❌ [{model_name}] {source_name} 第 {n_idx} 条分析失败")
                except Exception as e:
                    logger.error(f"❌ [{model_name}] {source_name} 第 {n_idx} 条分析异常: {e}", exc_info=True)

                if n_idx < len(news_to_analyze):
                    await asyncio.sleep(2)

    log_crawl("=" * 50)
    log_crawl(f"✅ 所有新闻源抓取完成! 保存: {total_saved} 条, 分析: {total_analyzed} 条, 耗时: {int((datetime.now() - start_time).total_seconds())}秒")
    log_crawl("=" * 50)

    log_crawl("📊 开始指数监控...")
    await crawl_indices()
    log_crawl("✅ Finnhub 指数监控完成")

    log_crawl("=" * 50)
    log_crawl(f"✅ 所有新闻源抓取完成! 保存: {total_saved} 条, 分析: {total_analyzed} 条, 耗时: {int((datetime.now() - start_time).total_seconds())}秒")
    log_crawl("=" * 50)

    log_crawl("=" * 50)
    log_crawl("📊 开始指数监控...")
    log_crawl("=" * 50)
    await crawl_indices()
    log_crawl("=" * 50)
    log_crawl("✅ Finnhub 指数监控完成")
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