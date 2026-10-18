import logging
import asyncio
from typing import List, Callable, Optional, Dict, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app import crud, schemas
from app import models
from app.crawlers import (
    EastmoneyDepthCrawler,
    THSNewsCrawler,
    SinaNewsCrawler,
    NewsItem
)
from app.crawlers.market_data import refresh_market_data
from app.utils.anti_crawl import (
    check_task_allowed, mark_task_started, mark_task_finished,
)
from app.utils.macro_refresh import refresh_snapshot
from app.utils.stock_temp_refresh import refresh_snapshot as refresh_stock_temp_snapshot
from app.utils.hot_sector_refresh import fetch_and_store_hot_sector

logger = logging.getLogger(__name__)

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
# 定时任务统一用**北京时间**：
#   - 10:00 morning  盘前一轮（新闻 + 今日热点）
#   - 16:00 close    收盘一轮（全部页面数据，当天以收盘值为准）
CN_TZ = ZoneInfo("Asia/Shanghai")

# 轮次标识
SESSION_MORNING = "morning"   # 10:00 盘前
SESSION_CLOSE = "close"       # 16:00 收盘

scheduler = AsyncIOScheduler(timezone=TOKYO_TZ)

crawl_progress_callback: Optional[Callable] = None

CONCURRENT_CRAWLERS = 2


def _write_crawl_log(task: str, session: str, source: str = None,
                     item_count: int = 0, status: str = "ok",
                     error_message: str = None, duration: int = 0) -> None:
    """把一次抓取结果写入 crawl_logs 表（供 to_thread 调用或直接调用）。

    实际写入委托给 crud.write_crawl_log：云库启用时走PostgREST，否则走 SQLite。
    写日志失败不应影响抓取主流程，故内部吞掉异常。
    """
    db = None
    try:
        db = SessionLocal()
        crud.write_crawl_log(
            db,
            task=task, session=session, source=source,
            item_count=item_count or 0, status=status,
            error_message=error_message, duration=duration or 0,
            crawl_time=datetime.now(),
        )
    except Exception as e:
        logger.warning(f"写入 crawl_logs 失败（不影响抓取）: {e}")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


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


async def crawl_market_data(session: str = "manual"):
    log_crawl("=" * 50)
    log_crawl(f"开始更新市场行情数据... (轮次: {session})")
    log_crawl("=" * 50)

    t0 = datetime.now()
    try:
        data = await refresh_market_data()
        items = data.get("items", [])
        count = len(items)
        valid = sum(1 for x in items if x.get("value") is not None)
        log_crawl(f"市场行情更新完成: {count} 项指标 (有效 {valid} 项)")
        for item in items:
            log_crawl(f"  - {item['name']}: {item.get('value')} ({item.get('date')})")
        dur = int((datetime.now() - t0).total_seconds())
        _write_crawl_log(task="market", session=session, source=data.get("source", "market"),
                         item_count=valid, duration=dur,
                         status="ok" if valid else "empty")
    except Exception as e:
        log_crawl(f"市场行情更新出错: {str(e)}")
        logger.error(f"!!! 市场行情更新出错: {e}", exc_info=True)
        _write_crawl_log(task="market", session=session, source="market",
                         item_count=0, status="failed",
                         error_message=str(e)[:500],
                         duration=int((datetime.now() - t0).total_seconds()))

    log_crawl("=" * 50)
    log_crawl("市场行情更新任务完成")
    log_crawl("=" * 50)


async def refresh_macro_indicators(session: str = "manual"):
    """抓取宏观指标快照（资金面 / 经济热度），写入最新值与历史表。

    与页面接口 /api/macro/refresh 共用 app/utils/macro_refresh.py 的实现，
    抓取是同步阻塞的（akshare + 央行官网），放到线程里执行避免卡住事件循环。

    限流：akshare 内部封装了请求，无法逐次插桩，故用任务级配额
    （TASK_DAILY_QUOTA）+ 防重入锁控制，避免页面手动刷新与定时任务叠加打爆来源。
    """
    if not check_task_allowed("macro"):
        log_crawl("宏观指标：已达今日配额或正在运行，跳过")
        _write_crawl_log(task="macro", session=session, source="macro",
                         item_count=0, status="skipped",
                         error_message="已达今日配额或正在运行")
        return

    log_crawl("=" * 50)
    log_crawl(f"开始更新宏观指标... (轮次: {session})")
    log_crawl("=" * 50)
    mark_task_started("macro")
    t0 = datetime.now()
    try:
        updated, errors, history_added = await asyncio.to_thread(refresh_snapshot)
        log_crawl(f"宏观指标更新完成: 快照 {updated} 项, 历史新增 {history_added} 条")
        for key, msg in (errors or {}).items():
            log_crawl(f"  ! {key} 抓取失败（保留上次读数）: {msg}")
        _write_crawl_log(task="macro", session=session, source="macro",
                         item_count=updated,
                         status="ok" if updated else "empty",
                         duration=int((datetime.now() - t0).total_seconds()))
    except Exception as e:
        log_crawl(f"宏观指标更新出错: {str(e)}")
        logger.error(f"!!! 宏观指标更新出错: {e}", exc_info=True)
        _write_crawl_log(task="macro", session=session, source="macro",
                         item_count=0, status="failed", error_message=str(e)[:500],
                         duration=int((datetime.now() - t0).total_seconds()))
    finally:
        mark_task_finished("macro")

    log_crawl("=" * 50)


async def refresh_stock_temp(session: str = "manual"):
    """抓取股票指标（A 股冷热三层温度计）快照，写入最新值与历史表。

    与页面接口 /api/stock-temp/refresh 共用 app/utils/stock_temp_refresh.py 的实现，
    抓取是同步阻塞的（akshare + 交易所官网 + 东财数据中心），放到线程里执行避免卡住事件循环。

    限流：同refresh_macro_indicators，任务级配额 + 防重入。
    """
    if not check_task_allowed("stock_temp"):
        log_crawl("股票指标：已达今日配额或正在运行，跳过")
        _write_crawl_log(task="stock_temp", session=session, source="stock_temp",
                         item_count=0, status="skipped",
                         error_message="已达今日配额或正在运行")
        return

    log_crawl("=" * 50)
    log_crawl(f"开始更新股票指标（三层温度计）... (轮次: {session})")
    log_crawl("=" * 50)
    mark_task_started("stock_temp")
    t0 = datetime.now()
    try:
        updated, errors, history_added = await asyncio.to_thread(refresh_stock_temp_snapshot)
        log_crawl(f"股票指标更新完成: 快照 {updated} 项, 历史新增 {history_added} 条")
        for key, msg in (errors or {}).items():
            log_crawl(f"  ! {key} 抓取失败（保留上次读数）: {msg}")
        _write_crawl_log(task="stock_temp", session=session, source="stock_temp",
                         item_count=updated,
                         status="ok" if updated else "empty",
                         duration=int((datetime.now() - t0).total_seconds()))
    except Exception as e:
        log_crawl(f"股票指标更新出错: {str(e)}")
        logger.error(f"!!! 股票指标更新出错: {e}", exc_info=True)
        _write_crawl_log(task="stock_temp", session=session, source="stock_temp",
                         item_count=0, status="failed", error_message=str(e)[:500],
                         duration=int((datetime.now() - t0).total_seconds()))
    finally:
        mark_task_finished("stock_temp")

    log_crawl("=" * 50)


async def refresh_hot_sector(session: str = "manual"):
    """抓取今日热点（概念板块热点前三）并落库，供后续复盘。

    与页面接口 /api/hot-sector 共用 app/utils/hot_sector_refresh.py 的实现。
    抓取本身是异步的（akshare 阻塞调用已在线程池内执行），这里直接 await，
    不套 asyncio.to_thread，避免「线程里再起事件循环」。

    落库按 (trade_date, board_code) 覆盖：同一交易日的板块数据会被后一次抓取
    就地更新，因此 16:00 收盘轮会自然覆盖 10:00 盘前轮的结果。

    限流：同 refresh_macro_indicators，任务级配额 + 防重入。
    """
    if not check_task_allowed("hot_sector"):
        log_crawl("今日热点：已达今日配额或正在运行，跳过")
        _write_crawl_log(task="hot_sector", session=session, source="hot_sector",
                         item_count=0, status="skipped",
                         error_message="已达今日配额或正在运行")
        return

    log_crawl("=" * 50)
    log_crawl(f"开始更新今日热点（概念板块）... (轮次: {session})")
    log_crawl("=" * 50)
    mark_task_started("hot_sector")
    t0 = datetime.now()
    stored = 0
    try:
        payload = await fetch_and_store_hot_sector()
        if payload.get("status") != "ok":
            log_crawl(f"今日热点更新失败: {payload.get('message')}")
            _write_crawl_log(task="hot_sector", session=session, source="hot_sector",
                             item_count=0, status="failed",
                             error_message=str(payload.get("message"))[:500],
                             duration=int((datetime.now() - t0).total_seconds()))
        else:
            stored = payload.get("stored_sectors", 0) or 0
            log_crawl(
                f"今日热点更新完成: 交易日 {payload.get('trade_date')}, "
                f"板块 {len(payload.get('sectors', []))} 个, "
                f"落库 {stored} 条"
            )
            for sector in payload.get("sectors", []):
                log_crawl(
                    f"  - {sector.get('rank')}. {sector.get('name')} "
                    f"{sector.get('change_percent')}% "
                    f"成分股 {sector.get('up_count')}涨{sector.get('down_count')}跌"
                )
            for msg in payload.get("errors") or []:
                log_crawl(f"  ! {msg}")
            _write_crawl_log(task="hot_sector", session=session, source="hot_sector",
                             item_count=stored,
                             status="ok" if stored else "empty",
                             duration=int((datetime.now() - t0).total_seconds()))
    except Exception as e:
        log_crawl(f"今日热点更新出错: {str(e)}")
        logger.error(f"!!! 今日热点更新出错: {e}", exc_info=True)
        _write_crawl_log(task="hot_sector", session=session, source="hot_sector",
                         item_count=0, status="failed", error_message=str(e)[:500],
                         duration=int((datetime.now() - t0).total_seconds()))
    finally:
        mark_task_finished("hot_sector")

    log_crawl("=" * 50)


async def full_crawl(session: str = "manual"):
    """抓取新闻。

    Args:
        session: 轮次标识 —— morning(10:00) / close(16:00) / manual(手动触发)，
            写入 crawl_logs 便于区分「盘前」与「收盘」两轮。
    """
    # 防重入：定时任务与页面手动触发可能撞在一起，重复打同一批来源
    if not check_task_allowed("news_crawl"):
        log_crawl("新闻抓取：正在运行中，跳过本次触发（防重入）")
        return

    log_crawl("=" * 50)
    log_crawl(f"开始执行新闻抓取任务... (轮次: {session})")
    log_crawl("=" * 50)
    mark_task_started("news_crawl")
    start_time = datetime.now()

    news_sources = [EastmoneyDepthCrawler, THSNewsCrawler, SinaNewsCrawler]
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

    elapsed = int((datetime.now() - start_time).total_seconds())
    for cls in news_sources:
        saved, src_news = source_results.get(cls.__name__, (0, []))
        # source_name 是类属性，直接取用，无需实例化
        src_label = cls.source_name
        if src_news:
            log_crawl(f"{src_label} 抓取到 {len(src_news)} 条新新闻")
        else:
            log_crawl(f"{src_label} 没有新新闻")
        _write_crawl_log(task="news", session=session, source=src_label,
                         item_count=saved, duration=elapsed,
                         status="ok" if saved else "empty")

    mark_task_finished("news_crawl")
    log_crawl("=" * 50)
    log_crawl(f"所有任务完成! 保存: {total_saved} 条, 耗时: {elapsed}秒")
    log_crawl("=" * 50)


async def refresh_financials(session: str = "manual", max_stocks: int = 10):
    """收盘轮：更新**已入库股票**的财务指标。

    财务数据是按股票代码查询的（无法全市场抓取），所以只更新此前已被用户
    抓取入库过的股票——即 `financial_reports` 表里出现过的 code。
    新股票仍需在「抓取控制」页手动输入代码抓取一次。
    """
    t0 = datetime.now()
    codes = await asyncio.to_thread(_list_tracked_stocks, max_stocks)
    if not codes:
        log_crawl("财务指标：库中暂无已跟踪股票，跳过（可在抓取控制页手动抓取）")
        _write_crawl_log(task="finance", session=session, source="finance",
                         item_count=0, status="skipped", error_message="无已跟踪股票")
        return

    log_crawl(f"财务指标：更新已入库的 {len(codes)} 只股票 -> {', '.join(c['code'] for c in codes)}")
    total = 0
    for item in codes:
        code, name = item["code"], item.get("name")
        try:
            saved = await asyncio.to_thread(_fetch_one_stock_finance, code, name)
            total += saved
            log_crawl(f"  - {code} {name or ''}: {saved} 期")
            _write_crawl_log(task="finance", session=session, source=f"{code}",
                             item_count=saved, status="ok" if saved else "empty",
                             duration=int((datetime.now() - t0).total_seconds()))
        except Exception as e:
            log_crawl(f"  ! {code} {name or ''} 抓取失败: {e}")
            logger.error(f"财务抓取 {code} 失败: {e}", exc_info=True)
            _write_crawl_log(task="finance", session=session, source=f"{code}",
                             item_count=0, status="failed", error_message=str(e)[:500])
    log_crawl(f"财务指标更新完成: 共 {total} 期")


def _list_tracked_stocks(limit: int = 10):
    """从 financial_reports 表取出已入库过的股票代码（去重）。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            models.FinancialReport.__table__.select()
            .with_only_columns(
                models.FinancialReport.stock_code,
                models.FinancialReport.stock_name,
            )
            .distinct()
            .limit(limit)
        ).fetchall()
        return [{"code": r[0], "name": r[1]} for r in rows]
    finally:
        db.close()


def _fetch_one_stock_finance(code: str, name: Optional[str]) -> int:
    """抓取并写入单只股票的财务数据（同步函数，供 to_thread 调用）。"""
    from getAkshare import fetch_financial_reports, reports_to_records

    df = fetch_financial_reports(code)
    records = reports_to_records(df)
    db = SessionLocal()
    try:
        return crud.upsert_financial_reports(db, code, records, stock_name=name)
    finally:
        db.close()


async def morning_session():
    """北京时间 10:00 —— 盘前一轮：抓新闻 + 今日热点。

    这一轮的结果是「盘前快照」，当天会被 16:00 收盘轮自然覆盖
    （新闻按 URL 累积、热点按 trade_date+board_code 覆盖）。
    """
    log_crawl("#" * 50)
    log_crawl("### 盘前轮次10:00：新闻 + 今日热点 ###")
    log_crawl("#" * 50)
    t0 = datetime.now()
    await full_crawl(session=SESSION_MORNING)
    await refresh_hot_sector(session=SESSION_MORNING)
    log_crawl(f"盘前轮次完成，总耗时 {int((datetime.now() - t0).total_seconds())} 秒")


async def close_session():
    """北京时间 16:00 —— 收盘一轮：抓取**所有页面**的数据并存入数据库。

    A 股 15:00 收盘，16:00 抓取即为当日最终收盘值。落库时：
    - 新闻：按 URL 累积去重（只补充盘前之后新发生的，不删除已有）
    - 行情：按 (symbol, date) 覆盖 → 当天记录被收盘值替换
    - 热点：按 (trade_date, board_code) 覆盖 → 当天板块被收盘值替换
    - 宏观 / 股票温度：更新最新值，历史表按日期追加
    因此「当日最终展示的数据」以本轮收盘结果为准。
    """
    log_crawl("#" * 50)
    log_crawl("### 收盘轮次 16:00：全部页面数据 ###")
    log_crawl("#" * 50)
    t0 = datetime.now()

    # 新闻先抓（依赖网络，快）
    await full_crawl(session=SESSION_CLOSE)
    # 财务指标：更新已入库股票（串行，逐只抓，避免并发打 akshare）
    await refresh_financials(session=SESSION_CLOSE)
    # 其余数据域并发抓取（akshare 类较慢），但每项内部已有防重入与配额保护
    results = await asyncio.gather(
        crawl_market_data(session=SESSION_CLOSE),
        refresh_macro_indicators(session=SESSION_CLOSE),
        refresh_stock_temp(session=SESSION_CLOSE),
        refresh_hot_sector(session=SESSION_CLOSE),
        return_exceptions=True,
    )
    for name, r in zip(
        ["市场行情", "宏观指标", "股票温度", "今日热点"], results
    ):
        if isinstance(r, Exception):
            log_crawl(f"  ! {name} 抓取异常: {r}")
            _write_crawl_log(task=name, session=SESSION_CLOSE, source=name,
                             item_count=0, status="failed", error_message=str(r)[:500])

    log_crawl("#" * 50)
    log_crawl(f"收盘轮次完成，总耗时 {int((datetime.now() - t0).total_seconds())} 秒")
    log_crawl("#" * 50)


def start_scheduler():
    if not scheduler.running:
        # ---- 10:00 盘前轮：新闻 + 今日热点 ----
        scheduler.add_job(
            morning_session,
            trigger=CronTrigger(hour='10', minute=0, timezone=CN_TZ),
            id='morning_session_job',
            name='Morning session at 10:00 CST (news + hot sectors)',
            replace_existing=True,
        )
        # ---- 16:00 收盘轮：全部页面数据 ----
        # A 股 15:00 收盘，16:00 抓当日最终收盘值；当天数据以本轮结果为准，
        # 会自然覆盖 10:00 盘前轮的同日记录（行情/热点按唯一键覆盖，新闻按 URL 累积）。
        scheduler.add_job(
            close_session,
            trigger=CronTrigger(hour='16', minute=0, timezone=CN_TZ),
            id='close_session_job',
            name='Close session at 16:00 CST (all pages)',
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "Scheduler started (Asia/Shanghai). "
            "10:00 盘前轮(新闻+今日热点) / 16:00 收盘轮(全部页面数据，当日以收盘值为准). "
            "History backfill is manual only (页面「补齐历史数据」按钮 / POST /api/*/backfill)."
        )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
