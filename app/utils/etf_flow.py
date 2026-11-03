#!/usr/bin/env python3
"""宽基 ETF 净流入（亿元）—— 抓取、回填、落库。

为什么要这个指标
================
指数点位只说明「价格涨了还是跌了」，说明不了「钱是进来还是出去」。
ETF 份额每天收盘后由交易所披露，**份额增量 × 当日收盘价**就是当天的净申购金额
（正 = 净流入，负 = 净赎回），这是业内最常用的免费口径（Wind/同花顺的
「ETF 资金净流入」也是这么估的，因为申赎明细不公开，只有份额）。

口径与边界
==========
- 样本 = 11 只主力宽基 ETF（沪深300 / 上证50 / 中证500 / 中证1000 / 科创50 /
  上证180 / 创业板 / 深证100），**不是全市场所有 ETF**。池子固定在
  ``BROAD_ETF_POOL``，改动会让「当日合计」出现跳变（当天算出的净流入只含
  变动的那几只），所以改池子必须挑一个非交易日、并重跑当日。
- 金额 = Σ(份额_t − 份额_{t−1}) × 收盘价_t，单位亿元。用收盘价代替净值，
  宽基 ETF 折溢价通常 < 0.3%，量级上不影响结论。
- 份额**折算/拆分**会让份额瞬间巨变（不是资金进出）。护栏：单只单日变动超过
  60% 直接判为折算并跳过该只当日贡献，计入 ``anomalies``，绝不写进合计。

数据源（2026-11-03 实测）
========================
1. 上交所 ``ak.fund_etf_scale_sse(date)`` —— **按单个日期**查询，返回当日全部
   ETF 份额（923 只，约 2 秒）。历史可回溯，今年 1 月 5 日就有数据。
2. 深交所 ``ak.fund_scale_daily_szse(start, end)`` —— 支持区间，但**单次响应
   有行数上限**（约 6.5 万行 = 2^16−1，超过就截断，表现为「区间前半段静默丢失」）。
   所以按月分段请求，绝不整年一次拿。
3. 价格 ``web.ifzq.gtimg.cn`` 日线（一次 400 根，够覆盖今年以来）。

落库
====
沿用 ``market_prices``（symbol = ``BROAD_ETF_FLOW``，unit=亿元），与 /market 的
美股四项、/a-share 的两个指数同表不同 symbol；那边都按白名单 symbol 查询，
互不干扰。策略同 ``index_daily``：只补缺失日期 + **当天永远重写**。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app import crud, schemas
from app.database import SessionLocal
from app.utils.index_daily import ytd_start

logger = logging.getLogger(__name__)

SYMBOL = "BROAD_ETF_FLOW"
NAME = "宽基ETF净流入"
UNIT = "亿元"

# 主力宽基 ETF：规模与成交都在前列，且覆盖「大盘 / 中盘 / 小盘 / 成长 / 科技」五个方向。
# market 决定去哪个交易所的接口取份额。
BROAD_ETF_POOL: List[Dict[str, str]] = [
    {"code": "510300", "name": "沪深300ETF", "market": "SH", "index": "沪深300"},
    {"code": "510050", "name": "上证50ETF", "market": "SH", "index": "上证50"},
    {"code": "510500", "name": "中证500ETF", "market": "SH", "index": "中证500"},
    {"code": "512100", "name": "中证1000ETF", "market": "SH", "index": "中证1000"},
    {"code": "588000", "name": "科创50ETF", "market": "SH", "index": "科创50"},
    {"code": "510180", "name": "上证180ETF", "market": "SH", "index": "上证180"},
    {"code": "159915", "name": "创业板ETF", "market": "SZ", "index": "创业板指"},
    {"code": "159919", "name": "沪深300ETF", "market": "SZ", "index": "沪深300"},
    {"code": "159922", "name": "中证500ETF", "market": "SZ", "index": "中证500"},
    {"code": "159845", "name": "中证1000ETF", "market": "SZ", "index": "中证1000"},
    {"code": "159901", "name": "深证100ETF", "market": "SZ", "index": "深证100"},
]

SH_CODES = tuple(x["code"] for x in BROAD_ETF_POOL if x["market"] == "SH")
SZ_CODES = tuple(x["code"] for x in BROAD_ETF_POOL if x["market"] == "SZ")
ALL_CODES = tuple(x["code"] for x in BROAD_ETF_POOL)

# 单只单日份额变动超过这个比例就认为是折算/拆分，不当成资金进出
SHARE_JUMP_LIMIT = 0.6

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_TX_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://gu.qq.com/"}


# ============================================================ 份额：上交所
def _fetch_sse_one(day: str) -> Tuple[str, Dict[str, float]]:
    """取一个交易日的上交所 ETF 份额：{code: 份额}（只保留池子里的）。"""
    import akshare as ak

    try:
        df = ak.fund_etf_scale_sse(date=day.replace("-", ""))
    except KeyError as exc:
        # 当天份额尚未披露时，akshare 内部是空框架 + df[[列名]] → KeyError，
        # 不是返回空表；按「未披露」处理，别当成故障刷日志
        raise RuntimeError(f"上交所 {day} 份额尚未披露") from exc
    if df is None or len(df) == 0:
        raise RuntimeError(f"上交所 {day} ETF 份额返回空")
    if "基金代码" not in df.columns:
        raise RuntimeError(f"上交所 {day} 份额尚未披露")
    sub = df[df["基金代码"].astype(str).isin(SH_CODES)]
    out: Dict[str, float] = {}
    for _, r in sub.iterrows():
        try:
            shares = float(r["基金份额"])
        except (TypeError, ValueError):
            continue
        if shares > 0:
            out[str(r["基金代码"])] = shares
    return day, out


def fetch_sse_shares(days: List[str], workers: int = 6) -> Tuple[Dict[str, Dict[str, float]], List[Dict[str, str]]]:
    """并发取多个交易日的沪市份额。返回 ({date: {code: 份额}}, 失败明细)。"""
    result: Dict[str, Dict[str, float]] = {}
    errors: List[Dict[str, str]] = []
    if not days:
        return result, errors
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = {pool.submit(_fetch_sse_one, d): d for d in days}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                day, shares = fut.result()
                if shares:
                    result[day] = shares
            except Exception as exc:  # noqa: BLE001 —— 单日失败不拖垮整轮
                errors.append({"date": d, "error": str(exc)[:160]})
                logger.warning("上交所 %s 份额抓取失败: %s", d, exc)
    return result, errors


# ============================================================ 份额：深交所
def _date_ranges(days: List[date], max_span: int = 45, max_gap: int = 10) -> List[Tuple[date, date]]:
    """把「需要的日期」合并成尽量少的连续区间。

    深市接口按区间取数，**响应体积决定耗时**（整月约 450 只 × 20 天 ≈ 9000 行、20 秒），
    所以增量时只抓真正需要的十几天，别为了省事整月整月地拉。
    上限 45 天 / 段：6.5 万行的响应上限下，45 天 ≈ 2 万行，安全。
    """
    if not days:
        return []
    out: List[Tuple[date, date]] = []
    start = prev = days[0]
    for d in days[1:]:
        too_far = (d - prev).days > max_gap
        too_long = (d - start).days >= max_span
        if too_far or too_long:
            out.append((start, prev))
            start = d
        prev = d
    out.append((start, prev))
    return out


def _fetch_szse_chunk(a: date, b: date) -> Dict[str, Dict[str, float]]:
    import akshare as ak

    df = ak.fund_scale_daily_szse(start_date=a.strftime("%Y%m%d"),
                                  end_date=b.strftime("%Y%m%d"), symbol="ETF")
    out: Dict[str, Dict[str, float]] = {}
    if df is None or len(df) == 0:
        return out
    sub = df[df["基金代码"].astype(str).isin(SZ_CODES)]
    for _, r in sub.iterrows():
        d = str(r["日期"])[:10]
        try:
            shares = float(r["基金份额"])
        except (TypeError, ValueError):
            continue
        if shares > 0:
            out.setdefault(d, {})[str(r["基金代码"])] = shares
    return out


def fetch_szse_ranges(ranges: List[Tuple[date, date]], workers: int = 2
                      ) -> Tuple[Dict[str, Dict[str, float]], List[Dict[str, str]]]:
    """按给定区间（并发）取深市份额。返回 ({date: {code: 份额}}, 失败明细)。"""
    result: Dict[str, Dict[str, float]] = {}
    errors: List[Dict[str, str]] = []
    if not ranges:
        return result, errors

    def _one(rng: Tuple[date, date]):
        return rng, _fetch_szse_chunk(*rng)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 3))) as pool:
        futures = {pool.submit(_one, r): r for r in ranges}
        for fut in as_completed(futures):
            try:
                (a, b), part = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append({"date": str(futures[fut]), "error": str(exc)[:160]})
                logger.warning("深交所 %s 份额抓取失败: %s", futures[fut], exc)
                continue
            for d, m in part.items():
                result.setdefault(d, {}).update(m)
    return result, errors


# ============================================================ 价格
def _tencent_code(code: str) -> str:
    return ("sh" if code.startswith("5") else "sz") + code


def _fetch_price_one(code: str, since: str) -> Dict[str, float]:
    resp = httpx.get(
        TENCENT_KLINE_URL,
        params={"param": f"{_tencent_code(code)},day,,,400,qfq"},
        headers=_TX_HEADERS, timeout=15, follow_redirects=True,
    )
    resp.raise_for_status()
    node = (resp.json() or {}).get("data", {}).get(_tencent_code(code)) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        d = str(row[0])[:10]
        if d < since:
            continue
        try:
            close = float(row[2])       # [date, open, close, high, low, volume]
        except (TypeError, ValueError):
            continue
        if close > 0:
            out[d] = close
    return out


def fetch_prices(since: str) -> Tuple[Dict[str, Dict[str, float]], List[Dict[str, str]]]:
    """11 只 ETF 的日收盘（并发）。顺带充当交易日历：返回的日期就是真实交易日。"""
    prices: Dict[str, Dict[str, float]] = {}
    errors: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_price_one, c, since): c for c in ALL_CODES}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                data = fut.result()
                if data:
                    prices[code] = data
            except Exception as exc:  # noqa: BLE001
                errors.append({"code": code, "error": str(exc)[:160]})
                logger.warning("ETF %s 价格抓取失败: %s", code, exc)
    return prices, errors


# ============================================================ 计算
def compute_flows(shares: Dict[str, Dict[str, float]],
                  prices: Dict[str, Dict[str, float]],
                  days: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按日算净流入（亿元）。返回 (flows, anomalies)。

    shares: {date: {code: 份额}}（两市已合并）
    prices: {code: {date: close}}
    days:   要计算的目标交易日（升序）
    """
    # 每只 ETF 自己的份额序列：折算/停牌会让某些日子缺值，前一值要按「该只自己的上一个有值日」取
    seq: Dict[str, List[Tuple[str, float]]] = {}
    for d, m in shares.items():
        for code, v in m.items():
            seq.setdefault(code, []).append((d, v))
    for code in seq:
        seq[code].sort(key=lambda x: x[0])

    flows: List[Dict[str, Any]] = []
    anomalies: List[Dict[str, Any]] = []
    for d in days:
        total = 0.0
        used = 0
        for meta in BROAD_ETF_POOL:
            code = meta["code"]
            rows = seq.get(code) or []
            cur: Optional[float] = None
            prev: Optional[float] = None
            for i, (dd, v) in enumerate(rows):
                if dd == d:
                    cur = v
                    prev = rows[i - 1][1] if i > 0 else None
                    break
            if cur is None or prev is None or prev <= 0:
                continue
            close = (prices.get(code) or {}).get(d)
            if not close:
                continue
            delta = cur - prev
            if abs(delta) / prev > SHARE_JUMP_LIMIT:
                # 份额折算/拆分，不是申赎；跳过并留痕
                anomalies.append({"date": d, "code": code, "name": meta["name"],
                                  "jump": round(delta / prev * 100, 1)})
                continue
            total += delta * close
            used += 1
        if used:
            flows.append({"date": d, "value": round(total / 1e8, 2), "codes": used})
    return flows, anomalies


# ============================================================ 落库
def refresh_etf_flow(since: Optional[str] = None, force: bool = False,
                     window: int = 10) -> Dict[str, Any]:
    """抓取并落库宽基 ETF 净流入。

    默认只重算**最近 window 个交易日**（增量，实测 30~40 秒：沪市按日 + 深市当月段 +
    11 只价格），外加**库里任何缺失的交易日**（历史缺口要能自愈）；
    force=True 才从 since（默认今年 1 月 1 日）整体回填 —— 沪市要按日请求约 200 次，
    走 HTTP 会超网关 60 秒，全量回填请用 ``backfill_etf_flow.py`` 本地跑。
    """
    since = since or ytd_start()
    today = date.today().isoformat()
    db = SessionLocal()
    try:
        have = set(crud.get_market_dates_since(db, since).get(SYMBOL, set()))

        # 价格一次取满今年（腾讯 400 根够用），顺带当交易日历 —— 不猜节假日
        prices, price_errors = fetch_prices(since)
        if not prices:
            raise RuntimeError("全部 ETF 价格抓取失败，无法计算净流入")
        calendar = sorted({d for m in prices.values() for d in m})
        if not calendar:
            raise RuntimeError(f"{since} 之后没有交易日数据")

        # 要重算哪些日子：① 最近 window 个交易日（当天临时值/新数据）
        # ② **库里任何缺失的交易日** —— 缺口不只在尾部：某天沪市超时会在几个月前
        #    留个洞，只按「最近 N 天」重算永远补不上（2026-11-03 回填就漏了 5 天）。
        if force or not have:
            recent_start = since
        else:
            span = max(window, 5) * 2
            recent_start = max(since, (date.fromisoformat(max(have)) - timedelta(days=span)).isoformat())
        targets = {d for d in calendar if recent_start <= d <= today}
        targets |= {d for d in calendar if since <= d <= today and d not in have}
        targets = sorted(targets)
        if not targets:
            return {
                "since": since, "window": 0, "fetched": 0, "added": 0,
                "latest": None, "pool": len(BROAD_ETF_POOL),
                "anomalies": [], "errors": [], "error": None,
            }

        # 每个目标日都要「它自己的前一个交易日」当差分基准
        pos = {d: i for i, d in enumerate(calendar)}
        sse_days = set()
        for d in targets:
            sse_days.add(d)
            i = pos[d]
            if i > 0:
                sse_days.add(calendar[i - 1])
        sse_days = sorted(sse_days)

        # 沪市（按日）与深市（按区间）互不依赖，必须并行 —— 串行时深市一段就要 10~20 秒，
        # 加在沪市后面会把手动刷新顶到网关 60 秒边缘（2026-11-03 实测串行 59 秒）
        ranges = _date_ranges([date.fromisoformat(d) for d in sse_days])
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_sse = pool.submit(fetch_sse_shares, sse_days)
            fut_szse = pool.submit(fetch_szse_ranges, ranges)
            sse_shares, sse_errors = fut_sse.result()
            szse_shares, szse_errors = fut_szse.result()

        # 交易所接口偶发读超时（实测沪市一天里会有一两次），失败的那天再试一轮；
        # 当天份额当晚才披露，重试它只是白等，直接跳过
        retry_days = [e["date"] for e in sse_errors if e.get("date") and e["date"] != today]
        if retry_days:
            logger.info("沪市 %d 天抓取失败，重试一轮: %s", len(retry_days), retry_days[:5])
            again, more_errors = fetch_sse_shares(retry_days, workers=3)
            sse_shares.update(again)
            sse_errors = [e for e in sse_errors if e.get("date") not in again] + more_errors

        retry_ranges = []
        for e in szse_errors:
            parts = str(e.get("date", "")).split("~")
            if len(parts) == 2:
                try:
                    retry_ranges.append((date.fromisoformat(parts[0]), date.fromisoformat(parts[1])))
                except ValueError:
                    continue
        if retry_ranges:
            logger.info("深市 %d 段抓取失败，重试一轮", len(retry_ranges))
            again, more_errors = fetch_szse_ranges(retry_ranges, workers=1)
            for d, m in again.items():
                szse_shares.setdefault(d, {}).update(m)
            szse_errors = more_errors

        if not sse_shares or not szse_shares:
            raise RuntimeError(
                f"份额数据缺失：沪市 {len(sse_shares)} 天 / 深市 {len(szse_shares)} 天")

        # 两市都有数据的日期才算数（只统计单边会让合计口径漂移）
        both = set(sse_shares) & set(szse_shares)
        shares: Dict[str, Dict[str, float]] = {}
        for d in both:
            merged = dict(szse_shares[d])
            merged.update(sse_shares[d])
            shares[d] = merged
        days_ok = [d for d in targets if d in both]
        if not days_ok:
            raise RuntimeError("目标区间内没有「沪深两市份额齐备」的交易日")

        flows, anomalies = compute_flows(shares, prices, days_ok)

        rows: List[schemas.MarketPriceCreate] = []
        for f in flows:
            if not force and f["date"] in have and f["date"] != today:
                continue
            rows.append(schemas.MarketPriceCreate(
                symbol=SYMBOL, name=NAME, unit=UNIT,
                value=f["value"], date=f["date"],
            ))
        saved = crud.upsert_market_prices(db, rows) if rows else 0
        latest = flows[-1] if flows else None
        logger.info("宽基 ETF 净流入：%d 天，写入 %d 行，最新 %s",
                    len(flows), saved, latest)

        return {
            "since": recent_start,
            "window": len(targets),
            "fetched": len(flows),
            "added": saved,
            "latest": latest,
            "pool": len(BROAD_ETF_POOL),
            "anomalies": anomalies[:20],
            # 当天份额当晚才披露，白天跑到它算预期行为，不刷进错误明细
            "errors": [e for e in (price_errors + sse_errors + szse_errors)
                       if not (e.get("date") == today and "披露" in str(e.get("error", "")))][:20],
            "missing": [d for d in targets if d not in both][:20],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 上抛给调度/接口统一记日志
        logger.error("宽基 ETF 净流入抓取失败: %s", exc, exc_info=True)
        return {
            "since": since, "window": 0, "fetched": 0, "added": 0,
            "latest": None, "pool": len(BROAD_ETF_POOL),
            "anomalies": [], "errors": [], "error": str(exc)[:300],
        }
    finally:
        db.close()
