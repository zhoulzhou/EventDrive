import logging
import asyncio
from typing import List, Callable, Optional, Dict, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
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
from app.crawlers.x_twitter import fetch_tweets
from app.utils.feishu_notifier import (
    dfcf_feishu_notify, cls_feishu_notify, nyt_feishu_notify, bbc_feishu_notify,
    doubao_feishu_notify, openrouter_feishu_notify, deepseek_feishu_notify,
    notify_index_alert, x_feishu_status_notify
)
from app.utils.doubao_analyzer import init_doubao_analyzer, get_doubao_analyzer
from app.utils.openrouter_analyzer import init_openrouter_analyzer, get_openrouter_analyzer
from app.utils.deepseek_analyzer import init_deepseek_analyzer, get_deepseek_analyzer

logger = logging.getLogger(__name__)

TOKYO_TZ = ZoneInfo("Asia/Tokyo")

scheduler = AsyncIOScheduler(timezone=TOKYO_TZ)

crawl_progress_callback: Optional[Callable] = None

CONCURRENT_CRAWLERS = 2


def set_crawl_progress_callback(callback: Callable):
    global crawl_progress_callback
    crawl_progress_callback = callback


def log_crawl(message: str):
    logger.info(message)
    if crawl_progress_callback:
        try:
            crawl_progress_callback(message)
        except Exception:
            pass


async def _db_execute(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def process_news_item(news_item: NewsItem):
    return schemas.NewsCreate(
        title=news_item.title,
        content=news_item.content,
        source=news_item.source,
        publish_time=news_item.publish_time,
        url=news_item.url,
        author=news_item.author,
        summary=news_item.summary,
        image_path=None
    )


async def crawl_single_source(crawler_class) -> Tuple[int, List[Dict[str, Any]]]:
    db = SessionLocal()
    saved_news = []
    saved_count = 0
    source_name = crawler_class.__name__
    try:
        crawler = crawler_class()
        source_name = crawler.source_name
        log_crawl(f"📰 开始抓取: {source_name}")

        news_items = await crawler.crawl()
        log_crawl(f"[{source_name}] 获取到 {len(news_items)} 条新闻，准备保存...")

        for idx, news_item in enumerate(news_items):
            log_crawl(f"[{source_name}] 处理第 {idx+1}/{len(news_items)} 条: {news_item.title[:30]}...")
            exists = await _db_execute(crud.is_news_exists, db, news_item.url)
            if not exists:
                news_create = await process_news_item(news_item)
                await _db_execute(crud.create_news, db, news_create)
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

        log_entry = schemas.CrawlLogCreate(
            source=crawler.source_name,
            news_count=saved_count,
            status=crawler.get_status(),
            error_message=crawler.error_message,
            duration=crawler.get_crawl_duration()
        )
        await _db_execute(crud.create_crawl_log, db, log_entry)

        log_crawl(f"🏁 {source_name} 抓取完成: 保存 {saved_count} 条")
        return saved_count, saved_news

    except Exception as e:
        log_crawl(f"❌ {source_name} 抓取出错: {str(e)}")
        logger.error(f"!!! {source_name} 抓取出错: {e}", exc_info=True)
        return 0, []
    finally:
        db.close()


async def crawl_with_semaphore(sem: asyncio.Semaphore, crawler_class) -> Tuple[str, int, List[Dict[str, Any]]]:
    async with sem:
        count, news = await crawl_single_source(crawler_class)
        return crawler_class.__name__, count, news


async def crawl_indices():
    log_crawl("=" * 50)
    log_crawl("📊 开始执行指数监控任务...")
    log_crawl("=" * 50)

    crawler = None
    try:
        crawler = FinnhubIndexCrawler()
        alert_message = await crawler.crawl()

        if alert_message and settings.INDEX_FEISHU_WEBHOOK_URL:
            log_crawl("📤 正在发送指数监控通知...")
            result = await asyncio.to_thread(notify_index_alert, alert_message)
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

    if settings.DEEPSEEK_API_KEY:
        try:
            init_deepseek_analyzer(
                api_key=settings.DEEPSEEK_API_KEY,
                model=settings.DEEPSEEK_MODEL,
                feishu_webhook_url=settings.DEEPSEEK_FEISHU_WEBHOOK_URL,
                keyword=settings.DEEPSEEK_KEYWORD
            )
            log_crawl("✅ DeepSeek大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"❌ DeepSeek分析器初始化失败: {e}", exc_info=True)

    doubao_analyzer = get_doubao_analyzer()
    openrouter_analyzer = get_openrouter_analyzer()
    deepseek_analyzer = get_deepseek_analyzer()

    news_sources = [EastmoneyDepthCrawler, CLSDepthCrawler, NYTDepthCrawler, BBCCrawler]
    sem = asyncio.Semaphore(CONCURRENT_CRAWLERS)

    log_crawl(f"🚀 并发抓取 {len(news_sources)} 个新闻源 (并发数: {CONCURRENT_CRAWLERS})")
    tasks = [crawl_with_semaphore(sem, cls) for cls in news_sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    source_results: Dict[str, Tuple[int, List[Dict[str, Any]]]] = {}
    total_saved = 0
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"并发抓取出错: {r}", exc_info=True)
            continue
        cls_name, count, news = r
        source_results[cls_name] = (count, news)
        total_saved += count

    async def _analyze_and_notify(news_list, analyzer, notifier_func, label, model_name=""):
        nonlocal total_analyzed
        if not news_list or not analyzer:
            return
        for news in news_list[:2]:
            title = news.get('title', '')
            summary = news.get('summary', '')
            source = news.get('source', label)
            log_crawl(f"🔍 [{label}] 正在分析: {title[:50]}...")
            try:
                result = await asyncio.to_thread(analyzer.analyze_only, title, summary, source)
                if result:
                    if model_name:
                        await asyncio.to_thread(notifier_func, title, result, source, model_name)
                    else:
                        await asyncio.to_thread(notifier_func, title, result, source)
                    log_crawl(f"✅ [{label}] 分析并推送成功")
                    total_analyzed += 1
                else:
                    log_crawl(f"❌ [{label}] 分析失败")
            except Exception as e:
                logger.error(f"[{label}] 分析异常: {e}", exc_info=True)
                log_crawl(f"❌ [{label}] 分析异常: {e}")
            await asyncio.sleep(2)

    total_analyzed = 0

    dfcf_count, dfcf_news = source_results.get("EastmoneyDepthCrawler", (0, []))
    if dfcf_news:
        await asyncio.to_thread(dfcf_feishu_notify, dfcf_news[:5], "东方财富")
        await _analyze_and_notify(dfcf_news, deepseek_analyzer, deepseek_feishu_notify, "DeepSeek")
        await _analyze_and_notify(dfcf_news, doubao_analyzer, doubao_feishu_notify, "豆包")
    else:
        log_crawl("📭 东方财富没有新新闻")

    cls_count, cls_news = source_results.get("CLSDepthCrawler", (0, []))
    if cls_news:
        await asyncio.to_thread(cls_feishu_notify, cls_news[:5], "财联社")
    else:
        log_crawl("📭 财联社没有新新闻")

    nyt_count, nyt_news = source_results.get("NYTDepthCrawler", (0, []))
    if nyt_news:
        await asyncio.to_thread(nyt_feishu_notify, nyt_news[:5], "纽约时报")
        if openrouter_analyzer:
            await _analyze_and_notify(
                nyt_news, openrouter_analyzer, openrouter_feishu_notify,
                "OpenRouter", openrouter_analyzer.last_used_model
            )
    else:
        log_crawl("📭 纽约时报没有新新闻")

    bbc_count, bbc_news = source_results.get("BBCCrawler", (0, []))
    if bbc_news:
        await asyncio.to_thread(bbc_feishu_notify, bbc_news[:5], "BBC")
    else:
        log_crawl("📭 BBC没有新新闻")

    log_crawl("=" * 50)
    log_crawl("🐦 X平台推文抓取")
    log_crawl("=" * 50)
    if settings.X_B_T and settings.X_LIST_ID:
        x_result = await asyncio.to_thread(fetch_tweets)
        x_msg = x_result.get("message", "")
        x_status = x_result.get("status", "error")
        x_push = x_result.get("push_message")
        log_crawl(f"[X] {x_msg} (状态: {x_status})")
        if x_push:
            await asyncio.to_thread(x_feishu_status_notify, x_push)
    else:
        log_crawl("⚠️ X平台未配置，跳过")

    log_crawl("=" * 50)
    log_crawl(f"✅ 所有任务完成! 保存: {total_saved} 条, 分析推送: {total_analyzed} 条, 耗时: {int((datetime.now() - start_time).total_seconds())}秒")
    log_crawl("=" * 50)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            full_crawl,
            trigger=CronTrigger(hour='8,12,16,20', minute=0, timezone=TOKYO_TZ),
            id='crawl_job_daily_4_times',
            name='Crawl at 8,12,16,20 JST',
            replace_existing=True
        )
        scheduler.add_job(
            crawl_indices,
            trigger=CronTrigger(hour='8,12,16,20', minute=0, timezone=TOKYO_TZ),
            id='index_crawl_job_daily_4_times',
            name='Crawl indices at 8,12,16,20 JST',
            replace_existing=True
        )
        scheduler.start()
        logger.info("Scheduler started. Crawl at 8,12,16,20 JST (Asia/Tokyo).")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
