import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends

from app.scheduler import (
    full_crawl,
    set_crawl_progress_callback,
    close_session,
    crawl_market_data,
    refresh_macro_indicators,
    refresh_stock_temp,
)
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()

crawl_status: Dict[str, any] = {
    "is_running": False,
    "last_run": None,
    "current_source": None,
    "current_step": None,
    "steps": [],
    "logs": []
}
crawl_task = None


def crawl_log_handler(message: str):
    import datetime
    crawl_status["logs"].append({
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "message": message
    })
    if len(crawl_status["logs"]) > 50:
        crawl_status["logs"] = crawl_status["logs"][-50:]


@router.post("/crawl/trigger")
async def trigger_crawl():
    global crawl_task
    logger.info("📢 收到手动抓取请求")

    if crawl_status["is_running"]:
        logger.warning("抓取任务已在进行中，拒绝新请求")
        return {"status": "already_running", "message": "Crawl is already in progress"}

    crawl_status["is_running"] = True
    crawl_status["current_source"] = None
    crawl_status["current_step"] = "正在初始化..."
    crawl_status["steps"] = []
    crawl_status["logs"] = []
    crawl_log_handler("🚀 开始抓取任务...")

    set_crawl_progress_callback(crawl_log_handler)

    async def run_crawl():
        try:
            crawl_log_handler("🕐 抓取任务开始执行...")
            crawl_status["current_step"] = "正在抓取各新闻源..."
            await full_crawl()
            crawl_log_handler("✨ 抓取任务执行完毕")
            crawl_status["current_step"] = "抓取完成"
        except Exception as e:
            crawl_log_handler(f"❌ 抓取出错: {str(e)}")
            logger.error(f"❌ 抓取任务执行出错: {e}", exc_info=True)
        finally:
            crawl_status["is_running"] = False
            crawl_status["current_source"] = None
            import datetime
            crawl_status["last_run"] = datetime.datetime.now().isoformat()
            logger.info(f"📅 最后抓取时间: {crawl_status['last_run']}")

    crawl_task = asyncio.create_task(run_crawl())

    logger.info("🚀 抓取任务已启动")
    crawl_log_handler("🚀 抓取任务已启动")
    return {"status": "started", "message": "Crawl has been triggered"}


@router.get("/crawl/status")
def get_crawl_status(auth: bool = Depends(require_auth)):
    response = {
        "is_running": crawl_status["is_running"],
        "last_run": crawl_status["last_run"],
        "current_source": crawl_status.get("current_source"),
        "current_step": crawl_status.get("current_step"),
        "steps": crawl_status.get("steps", []),
        "logs": crawl_status.get("logs", [])
    }
    return response


# ======================================================================
# 外部调度入口（供平台定时任务调用）
# ======================================================================
# 定时抓取：**仅北京时间 16:00 一轮**，抓取除财务外的所有页面数据。
#
# 背景：APScheduler 内嵌在 Web 进程里，而云沙箱会缩容/回收进程 ——
# job 只在进程启动那一刻算出 next_run_time，进程被回收后定时点不会补跑，
# 导致「/health 显示 scheduler_running=true 但定时任务从不执行」。
# 因此改为由**外部定时任务**（平台 automation）主动调本接口点火。
#
# 关键约束：云沙箱反向代理硬超时 60 秒，而一轮完整抓取要数分钟
# （legulegu 串行 6s 间隔，单域就要 6~7 分钟）。所以这里必须
# **点火后台线程后立即返回**，绝不能同步等待。
#
# 财务指标**不在**自动抓取范围内：按股票代码逐只查询、耗时且低频，
# 改为在财务页面手动抓取（POST /api/finance/fetch）。

# 并发控制：**按域加锁**，而不是整轮一把大锁。
#
# 2026-10-24 改：原先是一把全局锁，一次只允许跑一轮。结果是「股市热度」这个
# 单域 6~7 分钟（被 legulegu 限流时可达 15 分钟以上）的慢域一旦超时，
# 云端想单独补跑其余域也会被 409 挡回去 —— 一个慢域拖垮整轮。
# 现在改成记录「正在跑的域集合」，只要**域不重叠**就允许并行点火：
# 慢域（stock_temp）跑它的，快域（news/market/macro）跑自己的。
#
# 注意 `running` 字段仍表示「是否有任意一轮在跑」，保持 /daily-status 的既有语义。
_daily_refresh_lock = threading.Lock()
_running_domains: set = set()
_daily_refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "domains": [],          # 正在跑的域（便于排查「卡在哪个域」）
}

# 收盘轮（close_session）实际抓取的域。**不含 news**：新闻只在上午 09:15
# 由 eventdrive-morning-news 云函数抓一轮（2026-10-24 用户要求），
# 收盘轮不再重复抓，省掉一次完整的东财新闻翻页。
CLOSE_DOMAINS = ("market", "macro", "stock_temp")
# 可单独补跑的域集合（含 news —— 上午那轮失败时可手动补）
ALL_DOMAINS = ("news",) + CLOSE_DOMAINS

# 单个数据域的补跑入口：key -> 协程工厂。
# 为什么需要它：close_session 里各域并发，其中 legulegu 串行 6s 间隔，
# stock_temp 单域就要 6~7 分钟，而沙箱可能在任务跑到一半时回收进程 —— 整轮
# 会被打断（实测 10-19 丢了 stock_temp）。让外部定时任务能**按域单独补跑**，
# 就不必因为某个慢域失败而重跑全部。
_DOMAIN_TASKS = {
    "news": lambda: full_crawl(session="morning"),
    "market": lambda: crawl_market_data(session="close"),
    "macro": lambda: refresh_macro_indicators(session="close"),
    "stock_temp": lambda: refresh_stock_temp(session="close"),
}


def _kick_daily_refresh(session: str = "close", domains: Optional[List[str]] = None) -> bool:
    """启动一轮收盘抓取（后台线程）。

    返回 False 表示**请求的域里有正在跑的**（与已跑任务冲突），调用方应稍后重试。

    domains 为 None 时跑整轮 close_session（行情类三域，**不含新闻**）；
    传入列表则只跑指定数据域。指定域之间是**并发**执行的（gather），
    因此拿 `market,macro` 这个组合时总耗时取决于最慢的那一个，
    而不是两者相加 —— 云端等待窗口很紧，这个差别很关键。

    例外：显式传入的域恰好等于收盘轮三域时仍走 close_session，
    以保留「快域并发 → 慢域独占」的编排（慢域与快域并发会被拖垮，见 close_session 注释）。

    域之间互不重叠时允许并行点火（见 _running_domains 的注释）：
    stock_temp 在跑的同时，可以照常跑 news/market/macro。
    """
    targets: List[str] = list(CLOSE_DOMAINS) if domains is None else [
        d for d in domains if d in _DOMAIN_TASKS
    ]
    if not targets:
        return False

    with _daily_refresh_lock:
        conflict = [d for d in targets if d in _running_domains]
        if conflict:
            logger.warning("数据域 %s 正在执行，本次触发跳过", ", ".join(conflict))
            return False
        was_idle = not _running_domains
        _running_domains.update(targets)
        _daily_refresh_state.update({
            "running": True,
            # 空闲时开新一轮才重置时间轴；并行补跑不覆盖正在跑那轮的 started_at
            "started_at": datetime.now().isoformat() if was_idle
                          else _daily_refresh_state.get("started_at"),
            "error": None,
            "domains": sorted(_running_domains),
        })
        if was_idle:
            _daily_refresh_state["finished_at"] = None
            _daily_refresh_state["result"] = None

    def _worker():
        # close_session 是 async，内部用 asyncio.to_thread 跑同步抓取，
        # 所以要在本线程里新建一个事件循环来跑，不能直接调。
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            if domains is None or set(targets) == set(CLOSE_DOMAINS):
                loop.run_until_complete(close_session())
                _daily_refresh_state["result"] = {
                    "mode": "full", "session": session, "ok": True,
                }
            else:
                # 单域失败不应连累其它域：逐个捕获异常，最后汇总
                async def _one(name: str):
                    try:
                        await _DOMAIN_TASKS[name]()
                        return name, None
                    except Exception as e:  # noqa: BLE001
                        logger.error("补跑数据域 %s 失败: %s", name, e, exc_info=True)
                        return name, str(e)[:200]

                done, failed = [], {}
                pairs = loop.run_until_complete(asyncio.gather(*[_one(n) for n in targets]))
                for name, err in pairs:
                    if err:
                        failed[name] = err
                    else:
                        done.append(name)
                _daily_refresh_state["result"] = {
                    "mode": "domains", "domains": targets,
                    "done": done, "failed": failed, "ok": not failed,
                }
        except Exception as e:
            logger.error("每日定时抓取失败: %s", e, exc_info=True)
            _daily_refresh_state["error"] = str(e)[:500]
        finally:
            try:
                loop.close()
            except Exception:
                pass
            with _daily_refresh_lock:
                _running_domains.difference_update(targets)
                _daily_refresh_state["running"] = bool(_running_domains)
                _daily_refresh_state["domains"] = sorted(_running_domains)
                _daily_refresh_state["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=_worker, name="daily-refresh", daemon=True).start()
    return True


@router.post("/crawl/daily-refresh")
async def trigger_daily_refresh(
    domains: Optional[str] = None,
    auth: bool = Depends(require_auth),
):
    """外部定时任务入口：跑一轮收盘抓取（行情 + 宏观 + 温度）。

    **不含新闻**：新闻只在上午 09:15 抓（`?domains=news`）。
    收盘轮传空即跑行情类三域。

    **立即返回、后台执行**：一轮完整抓取要数分钟，而云沙箱反代硬超时 60 秒，
    同步执行必然 504。落库由后台线程完成，稍后查 `/crawl/daily-status` 看进度。

    可选 `domains` 参数（逗号分隔）只跑指定数据域，用于补跑被中断的部分：
    `news,market,macro,stock_temp`

    注意：**不含财务指标**，财务在页面手动抓取（`POST /api/finance/fetch`）。
    """
    picked = None
    if domains:
        picked = [d.strip() for d in domains.split(",") if d.strip()]
        unknown = [d for d in picked if d not in _DOMAIN_TASKS]
        if unknown:
            return {
                "status": "error",
                "message": f"未知数据域: {', '.join(unknown)}；"
                           f"可选: {', '.join(_DOMAIN_TASKS)}",
            }

    started = _kick_daily_refresh(session="close", domains=picked)
    if not started:
        running = sorted(_running_domains)
        return {
            "status": "running",
            "message": f"请求的数据域与正在执行的任务重叠（当前在跑: {', '.join(running) or '无'}），"
                       f"本次跳过；请稍后重试或改用不重叠的 domains",
            "running_domains": running,
        }
    if picked:
        return {
            "status": "started",
            "message": f"已开始后台补跑: {', '.join(picked)}；"
                       f"稍后查询 /api/crawl/daily-status 看结果",
        }
    return {
        "status": "started",
        "message": "已开始后台抓取，约 3-5 分钟完成；稍后查询 /api/crawl/daily-status 看结果",
    }


@router.get("/crawl/daily-status")
def get_daily_refresh_status(auth: bool = Depends(require_auth)):
    """查询后台每日抓取的进度与结果。"""
    return dict(_daily_refresh_state)
