"""龙虎榜接口。

- GET  /api/lhb                  某交易日榜单一览（含打分、标签、告警、当日概览）
- GET  /api/lhb/detail           单条记录的买卖前五席位 + 该股历史上榜
- GET  /api/lhb/seat             单个营业部的实测画像（近 N 日兑现统计）
- GET  /api/lhb/seat-dict        席位类型说明 + 游资字典（文档口径，静态）
- POST /api/lhb/refresh          抓取并落库（后台线程，立即返回）
- GET  /api/lhb/refresh-status   后台抓取进度

数据**只从库里读**（GET 不触发任何外部请求）：龙虎榜是收盘后披露的日频数据，
由收盘轮定时任务（21:34）抓取落库；页面「刷新」按钮走 POST /refresh。

所有口径来自 docs/龙虎榜.txt 与 docs/龙虎榜案例.txt，实现见 app/utils/lhb.py。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.api.login import require_auth
from app.utils import lhb as engine
from app.utils import lhb_refresh

logger = logging.getLogger(__name__)
router = APIRouter()

CN_TZ = ZoneInfo("Asia/Shanghai")
DATA_STALE_HOURS = 26

_refresh_status: Dict[str, Any] = {"running": False, "last_result": None}


# ---------------------------------------------------------------- 小工具

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_str(ts: Optional[datetime]) -> str:
    if ts is None:
        return "—"
    return ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")


def _json(value, default):
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _field(row, key, default=None):
    """兼容 ORM 实例与 cloud_db.Row。"""
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:  # noqa: BLE001
        return getattr(row, key, default)


def _record_payload(row) -> Dict[str, Any]:
    combo = _json(_field(row, "combo_tags"), [None, None])
    tags = combo if isinstance(combo, list) else [combo, None]
    return {
        "trade_date": _field(row, "trade_date"),
        "trade_id": _field(row, "trade_id"),
        "code": _field(row, "code"),
        "name": _field(row, "name"),
        "market": _field(row, "market"),
        "explanation": _field(row, "explanation"),
        "scope": _field(row, "scope"),
        "close": _field(row, "close"),
        "change_rate": _field(row, "change_rate"),
        "turnover_rate": _field(row, "turnover_rate"),
        "free_mcap": _field(row, "free_mcap"),
        "accum_amount": _field(row, "accum_amount"),
        "buy_amt": _field(row, "buy_amt"),
        "sell_amt": _field(row, "sell_amt"),
        "net_amt": _field(row, "net_amt"),
        "deal_ratio": _field(row, "deal_ratio"),
        "buy1_ratio": _field(row, "buy1_ratio"),
        "inst_net": _field(row, "inst_net"),
        "north_net": _field(row, "north_net"),
        "hot_net": _field(row, "hot_net"),
        "combo_tag": tags[0] if tags else None,
        "combo_note": tags[1] if len(tags) > 1 else None,
        "seat_types": _json(_field(row, "seat_types"), {}),
        "score": _field(row, "score"),
        "grade": _field(row, "grade"),
        "alerts": _json(_field(row, "alerts"), []),
        "reasons": _json(_field(row, "reasons"), []),
        "position": _field(row, "position"),
        "pos_pct": _field(row, "pos_pct"),
        "streak": _field(row, "streak"),
    }


def _seat_payload(row) -> Dict[str, Any]:
    meta = _json(_field(row, "seat_tags"), {})
    stype = _field(row, "seat_type") or "other"
    label = engine.SEAT_TYPES.get(stype, engine.SEAT_TYPES["other"])
    return {
        "flag": _field(row, "flag"),
        "seq": _field(row, "seq"),
        "dept_code": _field(row, "dept_code"),
        "dept_name": _field(row, "dept_name"),
        "buy": _field(row, "buy"),
        "sell": _field(row, "sell"),
        "net": _field(row, "net"),
        "seat_type": stype,
        "seat_type_label": label["label"],
        "seat_type_tone": label["tone"],
        "seat_type_note": label["note"],
        "alias": (meta or {}).get("alias"),
        "styles": (meta or {}).get("styles") or [],
        "tags": (meta or {}).get("tags") or [],
    }


def _stats_of(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """页面概览：直接从落库后的记录聚合（不再回抓）。"""
    def _num(v) -> float:
        return float(v) if isinstance(v, (int, float)) else 0.0

    net = sum(_num(r.get("net_amt")) for r in engine.one_row_per_stock(records))
    inst = sum(_num(r.get("inst_net")) for r in engine.one_row_per_stock(records))
    north = sum(_num(r.get("north_net")) for r in engine.one_row_per_stock(records))
    alerts = sum(len(r.get("alerts") or []) for r in records)
    return {
        "rows": len(records),
        "stocks": len({r.get("code") for r in records}),
        "net_amt": round(net, 2),
        "inst_net": round(inst, 2),
        "north_net": round(north, 2),
        "alerts": alerts,
        "high_position": sum(1 for r in records if r.get("position") == "高位"),
        "lhasa": sum(1 for r in records if r.get("combo_tag") == "拉萨扎堆"),
        "three_day": sum(1 for r in records if r.get("scope") == "3day"),
        "inst_out": sum(1 for r in records if r.get("combo_tag") == "机构出逃·游资接盘"),
        "observe": sum(1 for r in records if (r.get("score") or 0) >= 60),
    }


# ---------------------------------------------------------------- 读取

@router.get("/lhb")
def get_lhb(date: Optional[str] = None,
            min_score: Optional[int] = None,
            grade: Optional[str] = None,
            only_alert: bool = False,
            db: Session = Depends(get_db),
            auth: bool = Depends(require_auth)):
    """某交易日龙虎榜一览。date 省略时取最近有数据的交易日。"""
    try:
        dates = crud.get_lhb_dates(db, limit=40)
        target = date or (dates[0] if dates else None)
        if not target:
            return {"status": "ok", "date": None, "dates": [], "records": [],
                    "stats": _stats_of([]), "updated_at": "—", "stale": False,
                    "note": "库里还没有龙虎榜数据，先点「抓取最新」拉取最近交易日"}

        rows = crud.get_lhb_records(db, target, min_score=min_score,
                                    grade=grade, only_alert=only_alert)
        records = [_record_payload(r) for r in rows]
        written = crud.get_lhb_last_updated(db)
        age = (_utcnow() - written).total_seconds() / 3600 if written else None
        return {
            "status": "ok",
            "date": target,
            "dates": dates,
            "records": records,
            "stats": _stats_of(records),
            "filters": {"min_score": min_score, "grade": grade,
                        "only_alert": only_alert},
            "updated_at": _local_str(written),
            "data_age_hours": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age >= DATA_STALE_HOURS),
            "source": "cloud" if crud.using_cloud_db() else "sqlite",
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("读取龙虎榜失败: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}


@router.get("/lhb/detail")
def get_lhb_detail(date: str, trade_id: str,
                   db: Session = Depends(get_db),
                   auth: bool = Depends(require_auth)):
    """单条记录详情：买卖前五席位（含类型/风格标签）+ 该股历史上榜。"""
    try:
        row = crud.get_lhb_record(db, date, trade_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        record = _record_payload(row)
        seats = [_seat_payload(s) for s in crud.get_lhb_seats(db, date, trade_id)]
        history = []
        for h in crud.get_lhb_stock_history(db, record["code"], limit=15):
            history.append({
                "trade_date": _field(h, "trade_date"),
                "scope": _field(h, "scope"),
                "explanation": _field(h, "explanation"),
                "change_rate": _field(h, "change_rate"),
                "net_amt": _field(h, "net_amt"),
                "deal_ratio": _field(h, "deal_ratio"),
                "score": _field(h, "score"),
                "grade": _field(h, "grade"),
                "combo_tag": (_json(_field(h, "combo_tags"), [None]) or [None])[0],
            })
        # 买榜 / 卖榜拆开给前端，画「买五卖五对照图」时不必再分拣
        buy = [s for s in seats if s["flag"] == "buy"]
        sell = [s for s in seats if s["flag"] == "sell"]
        return {"status": "ok", "record": record, "buy": buy, "sell": sell,
                "history": history}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("读取龙虎榜详情失败: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}


@router.get("/lhb/seat")
def get_seat_profile(dept_name: str, days: int = 45,
                     db: Session = Depends(get_db),
                     auth: bool = Depends(require_auth)):
    """单个营业部的实测画像：近 N 日上榜次数、隔日兑现率、最近几笔记录。

    文档的要求是「标签只作初始值，用历史交易数据自动校验」，这个接口就是那个校验。
    """
    try:
        days = max(7, min(int(days or 45), 180))
        info = engine.classify_seat(dept_name)
        rows = crud.get_lhb_seat_history(db, dept_name, days=days)
        data = [{"trade_date": _field(r, "trade_date"), "code": _field(r, "code"),
                 "flag": _field(r, "flag"), "buy": _field(r, "buy"),
                 "sell": _field(r, "sell"), "net": _field(r, "net")} for r in rows]
        stats = engine.seat_stats_from_rows(data)
        recent = sorted(data, key=lambda r: r["trade_date"], reverse=True)[:12]
        return {
            "status": "ok",
            "dept_name": dept_name,
            "type": info["type"],
            "type_label": engine.SEAT_TYPES[info["type"]]["label"],
            "alias": info["alias"],
            "styles": info["styles"],
            "tags": info["tags"],
            "window_days": days,
            "stats": stats,
            "recent": recent,
            "note": ("实测统计基于本系统已落库的历史（自启用起累积），样本少时仅供参考；"
                     "静态标签会过时，游资会换马甲、分仓。"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("读取席位画像失败: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}


@router.get("/lhb/seat-dict")
def get_seat_dict(auth: bool = Depends(require_auth)):
    """席位类型说明 + 游资字典 + 打分规则（全部静态，来自两份文档）。"""
    return {
        "status": "ok",
        "types": [
            {"key": key, **meta} for key, meta in engine.SEAT_TYPES.items()
            if key != "other"
        ],
        "depots": [
            {"name": item["name"], "aliases": item["keywords"],
             "styles": item["style"]} for item in engine.DEPOT_LIBRARY
        ],
        "thresholds": {
            "one_day_styles": list(engine.ONE_DAY_STYLES),
            "relay_styles": list(engine.RELAY_STYLES),
        },
    }


# ---------------------------------------------------------------- 抓取

def _kick_refresh(days: int, date: Optional[str]) -> bool:
    if _refresh_status.get("running"):
        return False
    _refresh_status.update({"running": True,
                            "started_at": datetime.now().isoformat(),
                            "last_result": None})
    _refresh_status.pop("error", None)

    def _worker():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            if date:
                res = lhb_refresh.refresh_day(db, date)
                payload = {"days": [res], "records": res["records"],
                           "errors": res.get("errors") or []}
            else:
                payload = lhb_refresh.refresh_recent(db, days=days)
            _refresh_status["last_result"] = payload
            logger.info("龙虎榜抓取完成: %s 天, %s 条",
                        len(payload.get("days") or []), payload.get("records"))
        except Exception as exc:  # noqa: BLE001
            logger.error("龙虎榜抓取失败: %s", exc, exc_info=True)
            _refresh_status["error"] = str(exc)[:300]
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            _refresh_status["running"] = False
            _refresh_status["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=_worker, name="lhb-refresh", daemon=True).start()
    return True


@router.post("/lhb/refresh")
def refresh_lhb(days: int = 1, date: Optional[str] = None,
                auth: bool = Depends(require_auth)):
    """抓取并落库（**立即返回、后台执行**）。

    单日约 4 次榜单/席位请求 + 每只股票 1 次日线（70 只 ≈ 10 秒），
    但在云沙箱里可能更慢，而反代硬超时 60 秒 —— 所以一律后台跑，页面轮询状态。
    """
    if not _kick_refresh(days=max(1, min(days, 20)), date=date):
        return {"status": "running", "message": "已有抓取任务在执行，请稍候"}
    target = date or f"最近 {max(1, min(days, 20))} 个交易日"
    return {"status": "started", "message": f"已开始后台抓取：{target}，稍后刷新页面查看"}


@router.get("/lhb/refresh-status")
def refresh_status(auth: bool = Depends(require_auth)):
    return dict(_refresh_status)
