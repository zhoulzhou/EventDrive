"""龙虎榜落库：抓取 → 打分 → 写云库。

页面（/lhb）与定时任务共用这里的一份实现，抓取逻辑全在 ``app/utils/lhb.py``，
本文件只负责「选交易日 → 落库 → 记账」。

三条与项目其它域一致的约定：
1. **只抓一个交易日**：龙虎榜是收盘后披露的日频数据，逐日抓、逐日覆盖，
   不做增量 diff（东财盘中会修正金额，重抓一次就把当天对齐）。
2. **按日先删后写**：见 crud.upsert_lhb_day 的注释。
3. **失败不静默**：返回 errors 列表，调用方写 crawl_logs（task="lhb"）。
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app import crud
from app.utils import lhb as engine

logger = logging.getLogger(__name__)

# 龙虎榜 18:00 左右披露完毕；回看窗口用于「今天没数据就退到上一个交易日」。
LOOKBACK_DAYS = 10


def _shift(d: str, days: int) -> str:
    return (_date.fromisoformat(d) + timedelta(days=days)).isoformat()


def latest_trade_date(anchor: Optional[str] = None) -> str:
    """最近的**有龙虎榜数据**的交易日（含 anchor 当天，向前回看 10 天）。

    为什么不查交易日历：东财对非交易日返回空列表，直接按「有数据」判定更省事，
    也不会因为节假日安排调整而失效（代价是最多 10 次空请求，只在无数据时发生）。
    """
    start = anchor or datetime.now().strftime("%Y-%m-%d")
    for i in range(LOOKBACK_DAYS + 1):
        d = _shift(start, -i)
        rows = engine.fetch_daily(d)
        if rows:
            return d
    return start


def refresh_day(db: Session, trade_date: Optional[str] = None,
                *, with_position: bool = True) -> Dict[str, Any]:
    """抓取并落库单个交易日的龙虎榜。

    with_position=False 用于批量回填：位置要逐只股票拉一次日 K（几十次请求/天），
    回填十几天的历史时成本会翻十倍，而历史日的位置对「复盘」意义有限。
    """
    day = trade_date or latest_trade_date()
    data = engine.build_day(day, with_position=with_position)
    result = crud.upsert_lhb_day(db, data["records"], data["seats"])
    return {
        "trade_date": day,
        "records": result["records"],
        "seats": result["seats"],
        "deleted": result["deleted"],
        "stats": data["stats"],
        "errors": data["errors"],
    }


def refresh_recent(db: Session, days: int = 1,
                   *, with_position: bool = True) -> Dict[str, Any]:
    """从最近交易日往前抓 days 天（自动跳过非交易日）。"""
    days = max(1, min(int(days or 1), 30))
    latest = latest_trade_date()
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    cursor = latest
    for _ in range(days):
        try:
            res = refresh_day(db, cursor, with_position=with_position)
            out.append(res)
            errors.extend(res.get("errors") or [])
        except Exception as exc:  # noqa: BLE001
            logger.error("龙虎榜 %s 落库失败: %s", cursor, exc, exc_info=True)
            errors.append(f"{cursor}: {exc}")
            break
        # 上一天没有数据就继续退，直到找到有数据的一天
        prev = None
        for i in range(1, LOOKBACK_DAYS + 1):
            d = _shift(cursor, -i)
            if engine.fetch_daily(d):
                prev = d
                break
        if not prev:
            break
        cursor = prev
    total_records = sum(r["records"] for r in out)
    return {
        "latest": latest,
        "days": out,
        "records": total_records,
        "errors": errors,
    }
