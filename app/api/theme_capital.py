"""主题资金接口：算力 / 人形机器人产业链的每日快照 + AI 归因。

页面只发一个 GET 拿整份快照（双链明细 + 趋势 + 归因），走 view_cache + ETag。
刷新是重活（40+ 次历史请求 + 两轮模型调用，约 40~70s），走后台线程点火后立即
返回，绝不放在请求里撞反代 60s 超时 —— 与 /api/crawl/daily-refresh 同一套路。
"""

import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from app import crud
from app.api.login import require_auth
from app.utils import http_cache
from app.utils import llm_summary as llm_mod
from app.utils import theme_capital as tc_mod
from app.utils import theme_capital_llm as tc_llm
from app.utils import view_cache

logger = logging.getLogger(__name__)

router = APIRouter()

# 刷新是非阻塞点火 + 独占：并发点两次会重复打东财（触发限流）和重复烧 token。
_refresh_lock = threading.Lock()
_refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None,
}


class _ApiKeyBody(BaseModel):
    api_key: str


@router.get("/theme-capital/snapshot")
def get_snapshot(request: Request, response: Response,
                 date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD，空取最新"),
                 auth: bool = Depends(require_auth)):
    """取主题资金快照（双链明细 + 趋势 + AI 归因）。整份响应走 view_cache + ETag。

    date 纳入 view_key，指定历史日与最新日不共用快照。
    """
    key = f"theme_capital:{date or 'latest'}"
    tag = http_cache.etag(key)
    early = http_cache.not_modified(request, tag)
    if early is not None:
        return early

    def build():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            record = crud.get_theme_capital_snapshot(db, date)
            dates = crud.list_theme_capital_dates(db, limit=40)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        if not record:
            return {
                "status": "empty",
                "message": "还没有生成主题资金快照。收盘轮（21:34）跑完会自动生成，"
                           "也可以在页面上点「刷新数据」。",
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

    result = view_cache.get_or_build(key, build)
    http_cache.apply_for(response, tag, result)
    return result


def _refresh_worker(date: Optional[str]) -> None:
    """后台线程：抓快照落库 → 两轮 AI 归因回填。独占锁由调用方持有到结束。"""
    from datetime import datetime
    from app.database import SessionLocal
    from app.scheduler import _write_crawl_log
    t0 = datetime.now()
    db = SessionLocal()
    try:
        summary = tc_mod.refresh_theme_capital(db, date)
        _refresh_state["last_result"] = summary
        if summary.get("status") == "ok":
            llm_result = tc_llm.generate_for_date(summary.get("market_date"))
            _refresh_state["last_result"]["llm"] = {
                "status": llm_result.get("status"),
                "message": llm_result.get("message"),
            }
        status = "ok" if summary.get("status") == "ok" else "empty"
        _write_crawl_log(task="theme_capital", session="manual",
                         source="eastmoney:theme_capital",
                         item_count=int(summary.get("item_count") or 0),
                         status=status,
                         duration=int((datetime.now() - t0).total_seconds()))
    except Exception as exc:  # noqa: BLE001
        logger.error("主题资金刷新异常: %s", exc, exc_info=True)
        _refresh_state["error"] = str(exc)[:500]
        try:
            from app.scheduler import _write_crawl_log
            _write_crawl_log(task="theme_capital", session="manual",
                             source="theme_capital", item_count=0, status="failed",
                             error_message=str(exc)[:500],
                             duration=int((datetime.now() - t0).total_seconds()))
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        _refresh_state["finished_at"] = datetime.now().isoformat()
        _refresh_state["running"] = False
        _refresh_lock.release()


@router.post("/theme-capital/refresh")
def refresh_snapshot(date: Optional[str] = Query(None, description="交易日，空取今天"),
                     auth: bool = Depends(require_auth)):
    """后台点火：抓一条新快照并生成 AI 归因。立即返回，前端轮询 status。"""
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "running",
                "message": "正在刷新中，请稍候",
                **{k: _refresh_state[k] for k in ("started_at", "finished_at")}}
    from datetime import datetime
    _refresh_state.update({
        "running": True,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "last_result": None,
        "error": None,
    })
    threading.Thread(target=_refresh_worker, args=(date,), daemon=True,
                     name="theme-capital-refresh").start()
    return {"status": "started", "message": "已在后台刷新，约需 1 分钟",
            "started_at": _refresh_state["started_at"]}


@router.get("/theme-capital/refresh-status")
def refresh_status(auth: bool = Depends(require_auth)):
    """刷新进度：前端每 3s 轮询，running 由 true 变 false 即结束。"""
    return {**_refresh_state}


# ================================================================ AI 归因
# Key 配置与今日热点共用（同一把运行时 Key），这里直接复用 llm_summary 的入口。
@router.get("/theme-capital/llm-config")
def llm_config(auth: bool = Depends(require_auth)):
    return llm_mod.config_view()


@router.post("/theme-capital/llm-config")
def llm_set_config(body: _ApiKeyBody, auth: bool = Depends(require_auth)):
    try:
        view = llm_mod.set_api_key(body.api_key)
    except llm_mod.LlmError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", **view}


@router.post("/theme-capital/llm-summary")
def make_llm_summary(date: Optional[str] = Query(None, description="交易日，空取最新"),
                     force: bool = Query(False, description="已有归因时是否强制重算"),
                     auth: bool = Depends(require_auth)):
    """两轮生成两条链的归因并落库。锁 + 缓存判定与收盘轮共用，不重复烧 token。"""
    return tc_llm.generate_for_date(date, force=force)
