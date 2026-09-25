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
    EastmoneyDepthCrawler,
    NYTDepthCrawler,
    BBCCrawler,
    NewsItem
)
from app.crawlers.x_twitter import fetch_tweets
from app.crawlers.market_data import refresh_market_data
from app.utils.macro_refresh import refresh_snapshot
from app.utils.stock_temp_refresh import refresh_snapshot as refresh_stock_temp_snapshot
from app.utils.hot_sector_refresh import fetch_and_store_hot_sector
from app.utils.feishu_notifier import (
    dfcf_feishu_notify, nyt_feishu_notify, bbc_feishu_notify,
    doubao_feishu_notify, openrouter_feishu_notify, deepseek_feishu_notify,
    x_feishu_status_notify
)
from app.utils.doubao_analyzer import init_doubao_analyzer, get_doubao_analyzer
from app.utils.openrouter_analyzer import init_openrouter_analyzer, get_openrouter_analyzer
from app.utils.deepseek_analyzer import init_deepseek_analyzer, get_deepseek_analyzer

logger = logging.getLogger(__name__)

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
# 数据抓取任务（市场行情 / 宏观指标 / 股票指标）统一用**北京时间**，
# 时刻由 start_scheduler() 指定；新闻抓取保持原有的 JST 节奏。
CN_TZ = ZoneInfo("Asia/Shanghai")

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
        log_crawl(f"开始抓取: {source_name}")

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
                log_crawl(f"[{source_name}] 保存成功 (累计: {saved_count})")
            else:
                log_crawl(f"[{source_name}] 已存在，跳过")

        log_crawl(f"{source_name} 抓取完成: 保存 {saved_count} 条")
        return saved_count, saved_news

    except Exception as e:
        log_crawl(f"{source_name} 抓取出错: {str(e)}")
        logger.error(f"!!! {source_name} 抓取出错: {e}", exc_info=True)
        return 0, []
    finally:
        db.close()


async def crawl_with_semaphore(sem: asyncio.Semaphore, crawler_class) -> Tuple[str, int, List[Dict[str, Any]]]:
    async with sem:
        count, news = await crawl_single_source(crawler_class)
        return crawler_class.__name__, count, news


async def crawl_market_data():
    log_crawl("=" * 50)
    log_crawl("开始更新市场行情数据...")
    log_crawl("=" * 50)

    try:
        data = await refresh_market_data()
        count = len(data.get("items", []))
        log_crawl(f"市场行情更新完成: {count} 项指标")
        for item in data.get("items", []):
            log_crawl(f"  - {item['name']}: {item.get('value')} ({item.get('date')})")
    except Exception as e:
        log_crawl(f"市场行情更新出错: {str(e)}")
        logger.error(f"!!! 市场行情更新出错: {e}", exc_info=True)

    log_crawl("=" * 50)
    log_crawl("市场行情更新任务完成")
    log_crawl("=" * 50)


async def refresh_macro_indicators():
    """抓取宏观指标快照（资金面 / 经济热度），写入最新值与历史表。

    与页面接口 /api/macro/refresh 共用 app/utils/macro_refresh.py 的实现，
    抓取是同步阻塞的（akshare + 央行官网），放到线程里执行避免卡住事件循环。
    """
    log_crawl("=" * 50)
    log_crawl("开始更新宏观指标...")
    log_crawl("=" * 50)

    try:
        updated, errors, history_added = await asyncio.to_thread(refresh_snapshot)
        log_crawl(f"宏观指标更新完成: 快照 {updated} 项, 历史新增 {history_added} 条")
        for key, msg in (errors or {}).items():
            log_crawl(f"  ! {key} 抓取失败（保留上次读数）: {msg}")
    except Exception as e:
        log_crawl(f"宏观指标更新出错: {str(e)}")
        logger.error(f"!!! 宏观指标更新出错: {e}", exc_info=True)

    log_crawl("=" * 50)


async def refresh_stock_temp():
    """抓取股票指标（A 股冷热三层温度计）快照，写入最新值与历史表。

    与页面接口 /api/stock-temp/refresh 共用 app/utils/stock_temp_refresh.py 的实现，
    抓取是同步阻塞的（akshare + 交易所官网 + 东财数据中心），放到线程里执行避免卡住事件循环。
    """
    log_crawl("=" * 50)
    log_crawl("开始更新股票指标（三层温度计）...")
    log_crawl("=" * 50)

    try:
        updated, errors, history_added = await asyncio.to_thread(refresh_stock_temp_snapshot)
        log_crawl(f"股票指标更新完成: 快照 {updated} 项, 历史新增 {history_added} 条")
        for key, msg in (errors or {}).items():
            log_crawl(f"  ! {key} 抓取失败（保留上次读数）: {msg}")
    except Exception as e:
        log_crawl(f"股票指标更新出错: {str(e)}")
        logger.error(f"!!! 股票指标更新出错: {e}", exc_info=True)

    log_crawl("=" * 50)


async def refresh_hot_sector():
    """抓取今日热点（概念板块热点前三）并落库，供后续复盘。

    与页面接口 /api/hot-sector 共用 app/utils/hot_sector_refresh.py 的实现。
    抓取本身是异步的（akshare 阻塞调用已在线程池内执行），这里直接 await，
    不套 asyncio.to_thread，避免「线程里再起事件循环」。
    """
    log_crawl("=" * 50)
    log_crawl("开始更新今日热点（概念板块）...")
    log_crawl("=" * 50)

    try:
        payload = await fetch_and_store_hot_sector()
        if payload.get("status") != "ok":
            log_crawl(f"今日热点更新失败: {payload.get('message')}")
        else:
            log_crawl(
                f"今日热点更新完成: 交易日 {payload.get('trade_date')}, "
                f"板块 {len(payload.get('sectors', []))} 个, "
                f"落库 {payload.get('stored_sectors', 0)} 条, "
                f"归因来源 {payload.get('reason_source')}"
            )
            for sector in payload.get("sectors", []):
                log_crawl(
                    f"  - {sector.get('rank')}. {sector.get('name')} "
                    f"{sector.get('change_percent')}% "
                    f"主力净流入 {sector.get('main_net_inflow')} 亿"
                )
            for msg in payload.get("errors") or []:
                log_crawl(f"  ! {msg}")
    except Exception as e:
        log_crawl(f"今日热点更新出错: {str(e)}")
        logger.error(f"!!! 今日热点更新出错: {e}", exc_info=True)

    log_crawl("=" * 50)


async def full_crawl():
    log_crawl("=" * 50)
    log_crawl("开始执行新闻抓取任务...")
    log_crawl("=" * 50)
    start_time = datetime.now()

    if settings.KB_API_KEY:
        try:
            init_doubao_analyzer(api_key=settings.KB_API_KEY, model=settings.KB_MODEL_ID, region=settings.KB_REGION)
            log_crawl("豆包大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"豆包分析器初始化失败: {e}", exc_info=True)

    if settings.OPENROUTER_API_KEY:
        try:
            init_openrouter_analyzer(api_key=settings.OPENROUTER_API_KEY)
            log_crawl("OpenRouter大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"OpenRouter分析器初始化失败: {e}", exc_info=True)

    if settings.DEEPSEEK_API_KEY:
        try:
            init_deepseek_analyzer(
                api_key=settings.DEEPSEEK_API_KEY,
                model=settings.DEEPSEEK_MODEL,
                feishu_webhook_url=settings.DEEPSEEK_FEISHU_WEBHOOK_URL,
                keyword=settings.DEEPSEEK_KEYWORD
            )
            log_crawl("DeepSeek大模型分析器初始化完成")
        except Exception as e:
            logger.error(f"DeepSeek分析器初始化失败: {e}", exc_info=True)

    doubao_analyzer = get_doubao_analyzer()
    openrouter_analyzer = get_openrouter_analyzer()
    deepseek_analyzer = get_deepseek_analyzer()

    news_sources = [EastmoneyDepthCrawler, NYTDepthCrawler, BBCCrawler]
    sem = asyncio.Semaphore(CONCURRENT_CRAWLERS)

    log_crawl(f"并发抓取 {len(news_sources)} 个新闻源 (并发数: {CONCURRENT_CRAWLERS})")
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
            log_crawl(f"[{label}] 正在分析: {title[:50]}...")
            try:
                result = await analyzer.analyze_only(title, summary, source)
                if result:
                    if model_name:
                        await notifier_func(title, result, source, model_name)
                    else:
                        await notifier_func(title, result, source)
                    log_crawl(f"[{label}] 分析并推送成功")
                    total_analyzed += 1
                else:
                    log_crawl(f"[{label}] 分析失败")
            except Exception as e:
                logger.error(f"[{label}] 分析异常: {e}", exc_info=True)
                log_crawl(f"[{label}] 分析异常: {e}")
            await asyncio.sleep(2)

    total_analyzed = 0

    dfcf_count, dfcf_news = source_results.get("EastmoneyDepthCrawler", (0, []))
    if dfcf_news:
        await dfcf_feishu_notify(dfcf_news[:5], "东方财富")
        await _analyze_and_notify(dfcf_news, deepseek_analyzer, deepseek_feishu_notify, "DeepSeek")
        await _analyze_and_notify(dfcf_news, doubao_analyzer, doubao_feishu_notify, "豆包")
    else:
        log_crawl("东方财富没有新新闻")

    nyt_count, nyt_news = source_results.get("NYTDepthCrawler", (0, []))
    if nyt_news:
        await nyt_feishu_notify(nyt_news[:5], "纽约时报")
        if openrouter_analyzer:
            await _analyze_and_notify(
                nyt_news, openrouter_analyzer, openrouter_feishu_notify,
                "OpenRouter", openrouter_analyzer.last_used_model
            )
    else:
        log_crawl("纽约时报没有新新闻")

    bbc_count, bbc_news = source_results.get("BBCCrawler", (0, []))
    if bbc_news:
        await bbc_feishu_notify(bbc_news[:5], "BBC")
    else:
        log_crawl("BBC没有新新闻")

    log_crawl("=" * 50)
    log_crawl("X平台推文抓取")
    log_crawl("=" * 50)
    if settings.X_B_T and settings.X_LIST_ID:
        x_result = await asyncio.to_thread(fetch_tweets)
        x_msg = x_result.get("message", "")
        x_status = x_result.get("status", "error")
        x_push = x_result.get("push_message")
        log_crawl(f"[X] {x_msg} (状态: {x_status})")
        if x_push:
            await x_feishu_status_notify(x_push)
    else:
        log_crawl("X平台未配置，跳过")

    log_crawl("=" * 50)
    log_crawl(f"所有任务完成! 保存: {total_saved} 条, 分析推送: {total_analyzed} 条, 耗时: {int((datetime.now() - start_time).total_seconds())}秒")
    log_crawl("=" * 50)


def start_scheduler():
    if not scheduler.running:
        # 新闻抓取：保持原有节奏（JST 8/12/16/20）
        scheduler.add_job(
            full_crawl,
            trigger=CronTrigger(hour='8,12,16,20', minute=0, timezone=TOKYO_TZ),
            id='crawl_job_daily_4_times',
            name='Crawl at 8,12,16,20 JST',
            replace_existing=True
        )
        # ---- 数据抓取三件套：市场行情 / 宏观指标 / 股票指标 ----
        # 统一在**北京时间 20:30**（2026-09-22 用户要求），每天各跑一次。
        # · 市场行情原先还挂在 full_crawl 末尾（跟着新闻一天跑 8 次），已摘出，只由这里触发；
        # · 宏观 / 股票只抓「当前读数」（顺带把读数变化记进历史表）；趋势页的长历史序列由页面
        #   「补齐历史数据」按钮触发（POST /api/*/backfill），不在定时任务里回溯；
        # · 20:30 在 A 股收盘之后，当日日频数据（成交额 / 换手率 / 两融 / 估值）都已落地；
        # · 三者同一时刻触发，会并发打外部接口（akshare / 交易所官网 / 央行），需要错开就改 minute。
        scheduler.add_job(
            crawl_market_data,
            trigger=CronTrigger(hour='20', minute=30, timezone=CN_TZ),
            id='market_crawl_job',
            name='Crawl market data at 20:30 CST',
            replace_existing=True
        )
        scheduler.add_job(
            refresh_macro_indicators,
            trigger=CronTrigger(hour='20', minute=30, timezone=CN_TZ),
            id='macro_snapshot_job',
            name='Refresh macro indicators at 20:30 CST',
            replace_existing=True
        )
        scheduler.add_job(
            refresh_stock_temp,
            trigger=CronTrigger(hour='20', minute=30, timezone=CN_TZ),
            id='stock_temp_snapshot_job',
            name='Refresh stock temperature at 20:30 CST',
            replace_existing=True
        )
        # ---- 今日热点：北京时间 20:35 抓取当日热点板块并落库 ----
        # 热点数据只在两个时机写入：用户点页面「刷新盘面」，或本任务。这里在收盘后补抓一次，
        # 保证每个交易日必定留下一条复盘记录（对同一交易日同一板块就地覆盖，取当日最终结果），
        # 页面打开时只读库展示，不发外部请求。
        # 排在 20:35 是为了避让 20:30 同时触发的市场行情/宏观/股票三件套，避免并发打外部接口。
        scheduler.add_job(
            refresh_hot_sector,
            trigger=CronTrigger(hour='20', minute=35, timezone=CN_TZ),
            id='hot_sector_snapshot_job',
            name='Refresh hot sectors at 20:35 CST',
            replace_existing=True
        )
        scheduler.start()
        logger.info(
            "Scheduler started. News crawl at 8,12,16,20 JST; "
            "market data + macro + stock indicators at 20:30 CST, hot sectors at 20:35 CST "
            "(Asia/Shanghai). "
            "History backfill is manual only (页面「补齐历史数据」按钮 / POST /api/*/backfill)."
        )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
