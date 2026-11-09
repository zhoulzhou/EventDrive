"""今日热点接口：读库为主，只在手动刷新时抓取。

页面只发一个请求（``GET /api/hotspot/today``）拿整份日报 —— 定调、主线、榜单、
资金地图、时间轴都在同一个 payload 里，理由见 app/utils/hotspot.py 顶部注释。
"""

import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app import crud
from app.database import SessionLocal
from app.api.login import require_auth
from app.utils import hotspot as hotspot_mod
from app.utils import hotspot_trend
from app.utils import llm_summary as llm_mod

logger = logging.getLogger(__name__)

router = APIRouter()

# 刷新是重活（7 次外部请求 + 若干库读），并发点两次没意义还可能触发东财限流
_refresh_lock = threading.Lock()


class _ApiKeyBody(BaseModel):
    api_key: str


@router.get("/hotspot/today")
def get_hotspot(date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，空取最新"),
                auth: bool = Depends(require_auth)):
    """取某交易日的热点日报。

    没有数据时返回 ``status="empty"`` 而不是 404 —— 上线首日、或收盘轮还没跑完时，
    页面需要区分「这一天还没生成」和「接口挂了」，前端据此显示不同的空态文案。
    """
    db = SessionLocal()
    try:
        record = crud.get_daily_hotspot(db, date)
        dates = crud.list_hotspot_dates(db, limit=40)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    if not record:
        return {
            "status": "empty",
            "message": "还没有生成热点日报。收盘轮（21:34）跑完会自动生成，"
                       "也可以在页面上点「重新生成」。",
            "dates": dates,
        }

    payload = record["payload"]
    return {
        "status": "ok",
        "date": record["market_date"],
        "generated_at": payload.get("generated_at"),
        "dates": dates,
        "payload": payload,
    }


@router.post("/hotspot/refresh")
def refresh_hotspot(date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，空取今天"),
                    auth: bool = Depends(require_auth)):
    """重新抓取并生成某交易日的热点日报（同步执行，约 10~25 秒）。

    为什么同步：整轮只有 7 次外部请求，最长也在反代 60 秒超时之内；
    同步能让页面直接拿到结果、当场看到刷新后的数字，比「先返回再轮询」体验好。
    并发放并发用一把锁挡住 —— 重跑会覆盖同一天的行，重复执行纯属浪费配额。
    """
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "running", "message": "正在生成中，请稍候再试"}

    db = SessionLocal()
    try:
        summary = hotspot_mod.refresh_hotspot(db, date)
        return {"status": "ok", "summary": summary}
    except hotspot_mod.HotspotError as exc:
        logger.error("热点刷新失败: %s", exc)
        return {"status": "error", "message": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001
        logger.error("热点刷新异常: %s", exc, exc_info=True)
        return {"status": "error", "message": f"生成失败：{str(exc)[:300]}"}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        _refresh_lock.release()


@router.get("/hotspot/dates")
def hotspot_dates(auth: bool = Depends(require_auth)):
    """已生成日报的交易日（新→旧），供页面日期切换。"""
    db = SessionLocal()
    try:
        return {"dates": crud.list_hotspot_dates(db, limit=60)}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/hotspot/trend")
def get_hotspot_trend(days: int = Query(60, ge=1, le=60,
                                        description="最近 N 份有日报的交易日，默认全部（上限 60）"),
                      auth: bool = Depends(require_auth)):
    """热点趋势：把历次「今日主线」摊成「行业 × 交易日」点阵。

    只读 daily_hotspots 快照（每交易日一行 JSON），不抓取、不建新表。
    两种粒度（主线大方向 / 细分行业）在 ``views`` 里各给一份 industries + points；
    页面已删掉区间 / 口径选择器，固定用 views.group + 全部可用交易日（60 上限），
    sector 仍保留供其它调用方复用。历史从该表有数据的那天算起，不回填更早日期。
    """
    db = SessionLocal()
    try:
        reports = crud.list_hotspot_reports(db, limit=days)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    data = hotspot_trend.build_trend(reports)
    return {"status": "ok", "source": "daily_hotspots", **data}


# ================================================================ AI 盘面解读
# 结论按交易日存在 daily_hotspots.payload.llm_summary 里，页面打开只读快照。
# 这里负责「手动生成」与「填 Key」；收盘轮由 scheduler.refresh_hotspot_llm
# 在日报落库后串行生成，两条链路共用 llm_summary.generate_for_date 的锁与缓存。

@router.get("/hotspot/llm-config")
def llm_config(auth: bool = Depends(require_auth)):
    """配置状态（只返回掩码，绝不回传完整 Key）。"""
    return llm_mod.config_view()


@router.post("/hotspot/llm-config")
def llm_set_config(body: _ApiKeyBody, auth: bool = Depends(require_auth)):
    """页面填写 DeepSeek API Key，立即在当前进程生效。"""
    try:
        view = llm_mod.set_api_key(body.api_key)
    except llm_mod.LlmError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", **view}


@router.post("/hotspot/llm-summary")
def make_llm_summary(date: Optional[str] = Query(None, description="交易日，空取最新一份"),
                     force: bool = Query(False, description="已有总结时是否强制重算"),
                     auth: bool = Depends(require_auth)):
    """把全量盘面原始证据 + 近 5 日档案发给 DeepSeek Flash，两轮生成归因分析并落库。

    同步执行：两轮各 20s 上限（关 thinking），最坏 40s + 连接开销，守住反代 60s；
    审稿轮失败会降级用初稿，不会让整份解读变成「生成失败」。
    """
    # 与收盘轮定时任务共用同一入口：同一把非阻塞锁 + 同一套 v2 缓存判定，
    # 页面连点、定时任务与守护线程并发时都不会重复烧 token。
    return llm_mod.generate_for_date(date, force=force)
