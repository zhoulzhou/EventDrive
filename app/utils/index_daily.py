#!/usr/bin/env python3
"""A 股指数日线（上证指数 / 科创50）—— 抓取、回填、落库。

为什么单独一个模块
==================
项目原有的「全球行情」页（/market）只做美股：纳斯达克100 / VIX / 美债收益率。
A 股宽基指数此前没有任何落库的地方（「指数预警」读的是 index/ 下的离线 CSV，
「股市热度」只取成交量、不存点位）。本模块把**上证指数（000001）**与
**科创50（000688）**的日收盘点位抓下来，写进 `market_prices` 表
（symbol = `SH000001` / `SH000688`），复用已有的 upsert 与查询链路，
不新建表、不动云库 schema。

数据源降级链
============
三个源实测读数完全一致（2026-11-02 收盘：上证 3638.03 / 科创50 1196.94），
按下面的顺序尝试，前一源失败才用后一个：

1. ``tencent``   ``web.ifzq.gtimg.cn`` 日线（一次 320 根，够覆盖今年以来）
2. ``sina``      akshare ``stock_zh_index_daily``（新浪指数日线，含全部历史）
3. ``eastmoney`` ``push2his.eastmoney.com`` kline（secid = 1.000001 / 1.000688）

为什么腾讯排第一（2026-11-02 实测）：同一天收盘价三源完全一致
（腾讯 3638.03 / 新浪 3638.031），但耗时差十倍 —— 腾讯 **0.5 秒**，
新浪 **5.8 秒**（一次回全历史 8700+ 根，akshare 逐行解析）。定时任务与
手动「抓取最新」都要跑它，所以快的放前面；新浪作为口径兜底（项目里
「股市热度」的成交额/换手率本来就用新浪指数日线，读数可以对得上）。
东财 ``push2his`` 实测会间歇性 ``Server disconnected``，只作最后一道。

落库策略
========
- 只写 ``since``（默认**今年 1 月 1 日**）之后的交易日，历史数据不会无限膨胀；
- 与库里已有的日期比对，**只写缺失的日期**；但**当天的行永远重写** ——
  盘中/早间抓到的可能是未收盘的临时点位，收盘轮（21:34）必须能把它覆盖成最终值。
- 三个源都失败时抛错（由调用方写 failed 日志），**绝不清空既有历史**。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from app import crud, schemas
from app.database import SessionLocal

logger = logging.getLogger(__name__)

# 展示顺序即抓取顺序；code 供新浪/腾讯用，secid 供东财用（1=沪市）
INDEX_SERIES: List[Dict[str, str]] = [
    {"symbol": "SH000001", "name": "上证指数", "code": "sh000001",
     "secid": "1.000001", "short": "上证"},
    {"symbol": "SH000688", "name": "科创50", "code": "sh000688",
     "secid": "1.000688", "short": "科创50"},
]

SYMBOLS = tuple(s["symbol"] for s in INDEX_SERIES)
UNIT = "点"

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_TX_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://gu.qq.com/"}
_EM_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://quote.eastmoney.com/"}


def ytd_start(today: Optional[date] = None) -> str:
    """今年 1 月 1 日（YYYY-01-01）。取到的第一个点自然就是今年的首个交易日。"""
    today = today or date.today()
    return f"{today.year}-01-01"


# ============================================================ 三个数据源
# 统一返回 [(date_str, close_float), ...]，升序；解析不出的一律跳过。
Point = Tuple[str, float]


def _from_sina(item: Dict[str, str], since: str) -> List[Point]:
    """新浪指数日线（akshare 封装）。一次返回全部历史，这里按 since 预过滤。"""
    import akshare as ak

    df = ak.stock_zh_index_daily(symbol=item["code"])
    if df is None or len(df) == 0:
        raise RuntimeError("新浪指数日线返回空")
    # 先按日期裁剪再逐行遍历：全表 8700+ 行，今年只用到最后 200 行
    try:
        keep = df["date"].astype(str).str[:10] >= since
        df = df[keep]
    except Exception:  # noqa: BLE001 —— 列名异常时退回全表逐行判断
        pass
    out: List[Point] = []
    for _, r in df.iterrows():
        d = str(r.get("date"))[:10]
        if not d or d < since:
            continue
        v = r.get("close")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append((d, v))
    return out


def _from_tencent(item: Dict[str, str], since: str) -> List[Point]:
    """腾讯日线：param=code,day,起,止,根数,qfq（qfq 对指数与不复权等价）。"""
    resp = httpx.get(
        TENCENT_KLINE_URL,
        params={"param": f"{item['code']},day,,,320,qfq"},
        headers=_TX_HEADERS, timeout=15, follow_redirects=True,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    node = (payload.get("data") or {}).get(item["code"]) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out: List[Point] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        d = str(row[0])[:10]
        if d < since:
            continue
        try:
            v = float(row[2])          # [date, open, close, high, low, volume]
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append((d, v))
    return out


def _from_eastmoney(item: Dict[str, str], since: str) -> List[Point]:
    """东财日线：f51 日期 / f53 收盘（fields2 顺序见下）。"""
    resp = httpx.get(
        EM_KLINE_URL,
        params={
            # f51 日期 f52 开 f53 收 f54 高 f55 低 f56 量 f57 额
            "secid": item["secid"], "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": 101, "fqt": 1, "end": "20500101", "lmt": 400,
        },
        headers=_EM_HEADERS, timeout=15, follow_redirects=True,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    rows = (payload.get("data") or {}).get("klines") or []
    out: List[Point] = []
    for line in rows:
        parts = str(line).split(",")
        if len(parts) < 3:
            continue
        d = parts[0][:10]
        if d < since:
            continue
        try:
            v = float(parts[2])
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append((d, v))
    return out


_SOURCES: Tuple[Tuple[str, Callable[[Dict[str, str], str], List[Point]]], ...] = (
    ("tencent", _from_tencent),
    ("sina", _from_sina),
    ("eastmoney", _from_eastmoney),
)

SOURCE_LABEL = {
    "sina": "新浪指数日线",
    "tencent": "腾讯日线",
    "eastmoney": "东财日线",
}


def fetch_index_daily(item: Dict[str, str], since: str) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, str]]]:
    """按降级链取一个指数的日线，返回 (points, 实际用的源, 失败明细)。"""
    today = date.today().isoformat()
    errors: List[Dict[str, str]] = []
    for name, fn in _SOURCES:
        try:
            raw = fn(item, since)
        except Exception as exc:  # noqa: BLE001 —— 单源失败只记一笔，继续降级
            errors.append({"source": name, "error": str(exc)[:200]})
            logger.warning("指数 %s 源 %s 抓取失败: %s", item["symbol"], name, exc)
            continue
        points = [{"date": d, "value": round(v, 3)}
                  for d, v in raw if since <= d <= today]
        if points:
            points.sort(key=lambda p: p["date"])
            return points, name, errors
        errors.append({"source": name, "error": f"{since} 之后无数据"})
    raise RuntimeError(
        f"{item['name']} 全部数据源均失败：" +
        "; ".join(f"{e['source']}={e['error']}" for e in errors)
    )


# ============================================================ 落库
def refresh_a_share_index(since: Optional[str] = None,
                          force: bool = False) -> Dict[str, Any]:
    """抓取并落库两个指数的日线。返回摘要（供调度日志与手动接口使用）。

    force=True：忽略「库里已有该日期」的判断，把 since 之后的所有点整体重写
    （合并口径修正后用）；默认只补缺口 + 重写当天。
    """
    since = since or ytd_start()
    today = date.today().isoformat()

    db = SessionLocal()
    try:
        have = crud.get_market_dates_since(db, since)   # {symbol: {date,...}}
        items: List[Dict[str, Any]] = []
        total_added = 0
        source_used: List[str] = []
        last_error: Optional[str] = None

        for meta in INDEX_SERIES:
            symbol = meta["symbol"]
            try:
                points, source, errors = fetch_index_daily(meta, since)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                items.append({
                    "symbol": symbol, "name": meta["name"], "ok": False,
                    "source": None, "fetched": 0, "added": 0, "errors": [],
                    "error": str(exc)[:300],
                })
                continue

            source_used.append(source)
            existing = have.get(symbol, set())
            rows: List[schemas.MarketPriceCreate] = []
            for p in points:
                # 当天永远重写：盘中抓到的可能是未收盘的临时点位，
                # 收盘轮要能把它覆盖成最终收盘值（见模块头注释「落库策略」）。
                if not force and p["date"] in existing and p["date"] != today:
                    continue
                rows.append(schemas.MarketPriceCreate(
                    symbol=symbol, name=meta["name"], unit=UNIT,
                    value=p["value"], date=p["date"],
                ))

            saved = crud.upsert_market_prices(db, rows) if rows else 0
            total_added += saved
            last = points[-1]
            logger.info("指数 %s：%d 个点，写入 %d 行（源 %s）",
                        symbol, len(points), saved, source)
            items.append({
                "symbol": symbol, "name": meta["name"], "ok": True,
                "source": source, "source_label": SOURCE_LABEL.get(source, source),
                "fetched": len(points), "added": saved,
                "latest": last, "since": since,
                "errors": errors,
            })

        if not source_used:
            raise RuntimeError(last_error or "全部指数抓取失败")

        return {
            "since": since,
            "items": items,
            "summary": {"added": total_added, "symbols": len(items)},
            "source": " / ".join(sorted(set(SOURCE_LABEL.get(s, s) for s in source_used))),
        }
    finally:
        db.close()
