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
from app.utils.feishu_notifier import notify_new_news, notify_nyt_news, notify_bbc_news, notify_em_news, notify_no_news, notify_index_alert, init_feishu_notifier, init_nyt_feishu_notifier, init_bbc_feishu_notifier, init_em_feishu_notifier, init_index_feishu_notifier
from app.utils.doubao_analyzer import init_doubao_analyzer, get_doubao_analyzer

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
        CLSDepthCrawler,
        EastmoneyDepthCrawler,
        NYTDepthCrawler,
        BBCCrawler
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
    log_crawl(f"✅ 前 {len(crawlers)} 个新闻源抓取完成! 保存: {total_saved} 条, 耗时: {duration}秒")
    log_crawl("=" * 50)

    # ==================== 第5个新闻源: Finnhub 指数监控 ====================
    log_crawl("=" * 50)
    log_crawl("📊 开始第 5 个新闻源: Finnhub 指数监控...")
    log_crawl("=" * 50)
    await crawl_indices()
    log_crawl("=" * 50)
    log_crawl("✅ Finnhub 指数监控完成")
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
                    elif 'bbc' in key.lower():
                        display_source = "BBC新闻"
                        notify_func = notify_bbc_news
                    elif 'eastmoney' in key.lower() or '东方财富' in key:
                        display_source = "东方财富"
                        notify_func = notify_em_news
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

    # ==================== 大模型分析新闻 ====================
    # 按新闻源分配不同的大模型和飞书配置
    # - 东方财富、纽约时报 → 豆包模型 → 新飞书(Talk)
    # - 财联社、BBC → OpenRouter模型 → 原飞书

    # 初始化豆包大模型（用于东方财富、纽约时报）
    if settings.KB_API_KEY:
        try:
            init_doubao_analyzer(
                api_key=settings.KB_API_KEY,
                model=settings.KB_MODEL_ID,
                region=settings.KB_REGION,
                feishu_webhook_url=settings.KB_FEISHU_WEBHOOK_URL,
                keyword=settings.KB_KEYWORD
            )
            log_crawl("✅ 豆包大模型分析器初始化完成 (东方财富、纽约时报)")
        except Exception as e:
            logger.error(f"❌ 豆包分析器初始化失败: {e}", exc_info=True)

    # 初始化OpenRouter大模型（用于财联社、BBC）
    if settings.OPENROUTER_API_KEY:
        try:
            init_knowledge_analyzer(
                api_key=settings.OPENROUTER_API_KEY,
                feishu_webhook_url=settings.OPENROUTER_FEISHU_WEBHOOK_URL,
                keyword=settings.OPENROUTER_KEYWORD
            )
            log_crawl("✅ OpenRouter大模型分析器初始化完成 (财联社、BBC)")
        except Exception as e:
            logger.error(f"❌ OpenRouter分析器初始化失败: {e}", exc_info=True)

    doubao_analyzer = get_doubao_analyzer()
    openrouter_analyzer = get_knowledge_analyzer()

    # 定义新闻源与大模型、飞书的映射关系
    doubao_sources = ["东方财富", "纽约时报"]
    openrouter_sources = ["财联社", "BBC"]

    if all_saved_news:
        log_crawl("=" * 50)
        log_crawl("🧠 开始大模型新闻分析...")
        log_crawl("=" * 50)

        # 按来源分组新闻
        news_by_source = {}
        for news in all_saved_news:
            source = news.get('source', '未知来源')
            if source not in news_by_source:
                news_by_source[source] = []
            news_by_source[source].append(news)

        # 分析每个新闻源的前两条新闻
        for source, news_list in news_by_source.items():
            if not news_list:
                continue

            news_to_analyze = news_list[:2]

            # 确定使用哪个分析器
            if source in doubao_sources:
                analyzer = doubao_analyzer
                model_name = "豆包"
            elif source in openrouter_sources:
                analyzer = openrouter_analyzer
                model_name = "OpenRouter"
            else:
                log_crawl(f"⚠️ 未知新闻源: {source}，跳过分析")
                continue

            if not analyzer:
                log_crawl(f"❌ {model_name}分析器未初始化，跳过{source}")
                continue

            for idx, news in enumerate(news_to_analyze, 1):
                news_title = news.get('title', '')
                news_content = news.get('content', news.get('summary', ''))

                log_crawl(f"🔍 [{model_name}] 正在分析 {source} 的第 {idx}/{len(news_to_analyze)} 条新闻: {news_title[:50]}...")

                try:
                    success = analyzer.analyze_and_push(
                        news_title=news_title,
                        news_content=news_content,
                        source=source
                    )
                    if success:
                        log_crawl(f"✅ [{model_name}] {source} 第 {idx} 条新闻分析并推送成功")
                    else:
                        log_crawl(f"❌ [{model_name}] {source} 第 {idx} 条新闻分析或推送失败")
                except Exception as e:
                    logger.error(f"❌ [{model_name}] {source} 第 {idx} 条新闻分析异常: {e}", exc_info=True)

                # 分析间隔
                if idx < len(news_to_analyze):
                    await asyncio.sleep(2)

        log_crawl("=" * 50)
        log_crawl("✅ 大模型新闻分析完成")
        log_crawl("=" * 50)
    else:
        logger.info("大模型分析跳过: 没有新保存的新闻")

    return total_saved


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