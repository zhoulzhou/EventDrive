import logging
import asyncio
from typing import List, Callable, Optional, Dict, Tuple, Any
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import SessionLocal
from app import crud, schemas
from app.crawlers import EastmoneyDepthCrawler, NewsItem
from app.crawlers.market_data import refresh_market_data
from app.utils.anti_crawl import (
    check_task_allowed, mark_task_started, mark_task_finished,
)
from app.utils.macro_refresh import refresh_snapshot
from app.utils.stock_temp_refresh import refresh_snapshot as refresh_stock_temp_snapshot
from app.utils import lhb_refresh as lhb_refresh_mod

logger = logging.getLogger(__name__)

# 定时抓取由**外部自动任务**调用 POST /api/crawl/daily-refresh 点火（北京时间 16:00）。
# 这里只保留 session 标识；时区常量随进程内 APScheduler 一并停用。
SESSION_CLOSE = "close"       # 16:00 收盘轮

# 进程内 APScheduler 实例 —— 已停用（云沙箱回收进程 → 定时任务不可靠），
# 仅保留对象供 /health 等旧调用方读取状态，不再注册任何 job。
scheduler = AsyncIOScheduler()

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
        if data.get("skipped"):
            log_crawl(f"市场行情无需抓取: {data.get('reason')}")
        else:
            log_crawl(f"市场行情更新完成: {count} 项指标 (有效 {valid} 项)")
        for item in items:
            saved = ((data.get("summary") or {}).get("per_symbol") or {}).get(item["symbol"]) or {}
            log_crawl(
                f"  - {item['name']}: {item.get('value')} ({item.get('date')}) "
                f"来源={'/'.join(item.get('sources') or ['-'])} 新增{saved.get('added', 0)}天"
            )
        dur = int((datetime.now() - t0).total_seconds())
        _write_crawl_log(task="market", session=session, source=data.get("source", "market"),
                         item_count=valid, duration=dur,
                         status="ok" if (valid or data.get("skipped")) else "empty")
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


async def refresh_lhb(session: str = "manual", days: int = 1):
    """抓取龙虎榜（榜单 + 买卖前五席位）并落库。

    龙虎榜是**收盘后披露**的日频数据（沪市约 18:00 齐），收盘轮 21:34 抓当日正合适。
    单日成本：4 次榜单/席位请求 + 每只上榜股 1 次日线（约 70 只，10~30 秒）。

    与页面「刷新」按钮共用 app/utils/lhb_refresh.py 的实现；
    限流按任务级配额 + 防重入（同 refresh_macro_indicators）。
    """
    if not check_task_allowed("lhb"):
        log_crawl("龙虎榜：已达今日配额或正在运行，跳过")
        _write_crawl_log(task="lhb", session=session, source="lhb",
                         item_count=0, status="skipped",
                         error_message="已达今日配额或正在运行")
        return

    log_crawl("=" * 50)
    log_crawl(f"开始更新龙虎榜... (轮次: {session})")
    log_crawl("=" * 50)
    mark_task_started("lhb")
    t0 = datetime.now()
    try:
        payload = await asyncio.to_thread(_refresh_lhb_sync, days)
        count = payload.get("records") or 0
        for d in payload.get("days") or []:
            log_crawl(f"  {d['trade_date']}: {d['records']} 条榜单 / "
                      f"{d['seats']} 条席位")
        for msg in (payload.get("errors") or [])[:5]:
            log_crawl(f"  ! {msg}")
        _write_crawl_log(task="lhb", session=session, source="lhb",
                         item_count=count, status="ok" if count else "empty",
                         duration=int((datetime.now() - t0).total_seconds()))
    except Exception as e:
        log_crawl(f"龙虎榜更新出错: {str(e)}")
        logger.error(f"!!! 龙虎榜更新出错: {e}", exc_info=True)
        _write_crawl_log(task="lhb", session=session, source="lhb",
                         item_count=0, status="failed", error_message=str(e)[:500],
                         duration=int((datetime.now() - t0).total_seconds()))
    finally:
        mark_task_finished("lhb")

    log_crawl("=" * 50)


def _refresh_lhb_sync(days: int = 1) -> dict:
    """在线程里跑：自己开 session（云库下 SessionLocal 只是壳，读写走 PostgREST）。"""
    db = SessionLocal()
    try:
        return lhb_refresh_mod.refresh_recent(db, days=days)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


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

    # 新闻源只保留东方财富（2026-10-24 用户要求下线同花顺、新浪财经两个源）。
    # 保留 list + 并发结构：将来加回源只需在此追加类，编排无需改动。
    news_sources = [EastmoneyDepthCrawler]
    sem = asyncio.Semaphore(CONCURRENT_CRAWLERS)

    log_crawl(f"并发抓取 {len(news_sources)} 个新闻源 (并发数: {CONCURRENT_CRAWLERS})")
    tasks = [crawl_with_semaphore(sem, cls) for cls in news_sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    source_results: Dict[str, Tuple[int, List[Dict[str, Any]]]] = {}
    failed_sources: Dict[str, str] = {}
    total_saved = 0
    # 按位置对齐回新闻源：gather 结果顺序与 tasks 一致，据此才能知道
    # 「是哪个源挂了」——只拿异常对象的话，落库时只能记一条无主的 failed。
    for cls, r in zip(news_sources, results):
        if isinstance(r, Exception):
            logger.error(f"并发抓取出错: {type(r).__name__}: {r}", exc_info=r)
            failed_sources[cls.__name__] = f"{type(r).__name__}: {r}"
            continue
        cls_name, count, news = r
        source_results[cls_name] = (count, news)
        total_saved += count

    elapsed = int((datetime.now() - start_time).total_seconds())
    for cls in news_sources:
        saved, src_news = source_results.get(cls.__name__, (0, []))
        # source_name 是类属性，直接取用，无需实例化
        src_label = cls.source_name
        if cls.__name__ in failed_sources:
            # 抓取抛异常 → 写 failed（2026-10-26）。原先这里会落一条 empty，
            # 把「源挂了」伪装成「今天没新新闻」，是排查时最容易骗人的一类假信号。
            log_crawl(f"{src_label} 抓取失败: {failed_sources[cls.__name__]}")
            _write_crawl_log(task="news", session=session, source=src_label,
                             item_count=0, status="failed",
                             error_message=failed_sources[cls.__name__][:500],
                             duration=elapsed)
            continue
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


async def close_session():
    """北京时间 16:00 —— 收盘一轮：抓取行情类数据并存入数据库。

    A 股 15:00 收盘，16:00 抓取即为当日最终收盘值。落库时：
    - 行情：按 (symbol, date) 覆盖 → 当天记录被收盘值替换
    - 宏观 / 股票温度：更新最新值，历史表按日期追加
    因此「当日最终展示的数据」以本轮收盘结果为准。

    **不含新闻**（2026-10-24 用户要求）：新闻只在上午 09:15 由
    `eventdrive-morning-news` 云函数抓一轮，收盘轮不再重复抓取。
    新闻域仍保留在 `/api/crawl/daily-refresh?domains=news` 的补跑入口里，
    需要时可手动补。

    **不含财务指标**：财务是按股票代码逐只查询的（无法全市场抓取），
    耗时且低频，改为在财务页面手动抓取（POST /api/finance/fetch）。
    """
    log_crawl("#" * 50)
    log_crawl("### 收盘轮次 16:00：行情类数据（不含新闻） ###")
    log_crawl("#" * 50)
    t0 = datetime.now()

    # 快域并发抓取（akshare 类较慢），但每项内部已有防重入与配额保护
    # 注：不含财务指标 —— 财务按股票逐只查询、耗时且低频，改在页面手动抓取
    fast_results = await asyncio.gather(
        crawl_market_data(session=SESSION_CLOSE),
        refresh_macro_indicators(session=SESSION_CLOSE),
        refresh_lhb(session=SESSION_CLOSE),
        return_exceptions=True,
    )
    for key, name, r in zip(
        ["market", "macro", "lhb"],
        ["市场行情", "宏观指标", "龙虎榜"],
        fast_results,
    ):
        if isinstance(r, Exception):
            log_crawl(f"  ! {name} 抓取异常: {r}")
            # task 必须写**英文域键**：云函数按 NEED=['market','macro',...] 查 crawl_logs
            # 判缺失，写中文名会让它以为该域「从没跑过」，从而重复补跑
            _write_crawl_log(task=key, session=SESSION_CLOSE, source=name,
                             item_count=0, status="failed", error_message=str(r)[:500])

    # 股市热度**单独跑、不与其他域并发**（2026-10-24 改）。
    #
    # 原先各域一起 gather，结果 10-21 起 stock_temp 连续三天「开始跑但从不落库」——
    # crawl_logs 里连一条 failed/skipped 都没有，说明进程在它跑完之前就没了。
    # 实测单跑 stock_temp 只要 260 秒，而与其它域并发时会拖到 700 秒以上乃至进程被杀。
    # 所以拆成「快域并发 → 慢域独占」，既降低峰值负载，也让慢域拿到干净的时间窗。
    stock_temp_result = await asyncio.gather(
        refresh_stock_temp(session=SESSION_CLOSE),
        return_exceptions=True,
    )
    for r in stock_temp_result:
        if isinstance(r, Exception):
            log_crawl(f"  ! 股票温度 抓取异常: {r}")
            _write_crawl_log(task="stock_temp", session=SESSION_CLOSE, source="股票温度",
                             item_count=0, status="failed", error_message=str(r)[:500])

    log_crawl("#" * 50)
    log_crawl(f"收盘轮次完成，总耗时 {int((datetime.now() - t0).total_seconds())} 秒")
    log_crawl("#" * 50)


def start_scheduler():
    """进程内 APScheduler —— 已停用，保留仅为兼容旧调用方。

    2026-10-19 起：云沙箱会缩容回收进程，而 APScheduler 的 job 只在进程启动
    那一刻算出 next_run_time、无持久化 job store，进程被回收后定时点不会补跑，
    导致「/health 显示 scheduler_running=true 但定时任务从不执行」。
    因此定时抓取改由**外部自动任务**调用 `POST /api/crawl/daily-refresh` 点火
    （见 app/api/crawl.py）。

    此函数**不再注册任何 job**，调用它只会打一条日志。
    """
    if not scheduler.running:
        logger.info(
            "进程内 APScheduler 已停用（云沙箱回收进程导致定时任务不可靠）。"
            "定时抓取改由外部自动任务调用 POST /api/crawl/daily-refresh 点火。"
        )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
