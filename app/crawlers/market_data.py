#!/usr/bin/env python3
"""市场数据抓取模块（海外权威数据源，均无需 API Key，适合日本服务器）。

数据源组合：
- 纳斯达克指数      -> 美国圣路易斯联储 FRED（NASDAQCOM）
- VIX 恐慌指数      -> CBOE 官方历史 CSV（换用 CBOE，时效好于 FRED）
- 美债 2Y/10Y 收益率 -> 美国财政部官方每日收益率 CSV（换用 Treasury，时效好于 FRED）

数据获取走「定时落库 + 页面读库」模式：
- 调度器调用 refresh_market_data() 抓取并写入 market_prices 表；
- 页面接口从数据库读取，不直接访问外部源。
"""
import asyncio
import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, List

import httpx

from app.database import SessionLocal
from app import crud, schemas
from app.market_strategy import advance

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(timeout=20)

# 各数据源端点
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)

# 指标定义：source 决定用哪个数据源，column 为财政部收益率 CSV 中的列名；unit 用于前端展示（收益率用 %）
SERIES = [
    {"key": "nasdaq", "name": "纳斯达克100指数", "symbol": "NASDAQ100", "unit": "", "source": "fred",
     "column": None, "describe": "NASDAQ-100 Index"},
    {"key": "vix", "name": "VIX恐慌指数", "symbol": "VIXCLS", "unit": "", "source": "cboe",
     "column": None, "describe": "CBOE Volatility Index"},
    {"key": "dgs2", "name": "美债2年期收益率", "symbol": "DGS2", "unit": "%", "source": "treasury",
     "column": "2 Yr", "describe": "U.S. Treasury 2-Year Yield"},
    {"key": "dgs10", "name": "美债10年期收益率", "symbol": "DGS10", "unit": "%", "source": "treasury",
     "column": "10 Yr", "describe": "U.S. Treasury 10-Year Yield"},
]


def _parse_ymd(value: str) -> str:
    """MM/DD/YYYY -> YYYY-MM-DD（与数据库 date 格式保持一致）。"""
    m, d, y = value.split("/")
    return f"{y}-{m}-{d}"


async def _fetch_fred(symbol: str) -> Dict[str, Any]:
    """抓取 FRED 序列，取 CSV 最后一行（最新日期），date 已是 YYYY-MM-DD。"""
    try:
        response = await _client.get(FRED_URL, params={"id": symbol})
        response.raise_for_status()
    except Exception as e:
        logger.error(f"获取 FRED 序列 {symbol} 失败: {e}", exc_info=True)
        return {"value": None, "date": None}

    rows = list(csv.reader(io.StringIO(response.text)))
    values = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date, raw = row[0].strip(), row[1].strip()
        if not raw or raw in (".", ""):
            continue
        try:
            values.append((date, float(raw)))
        except ValueError:
            continue

    if not values:
        return {"value": None, "date": None}
    date, value = values[-1]
    return {"value": value, "date": date}


async def _fetch_vix() -> Dict[str, Any]:
    """抓取 CBOE VIX 官方历史 CSV，取行尾（最新交易日）的 CLOSE 值。"""
    try:
        response = await _client.get(VIX_URL)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"获取 CBOE VIX 失败: {e}", exc_info=True)
        return {"value": None, "date": None}

    rows = list(csv.reader(io.StringIO(response.text)))
    # 表头: DATE,OPEN,HIGH,LOW,CLOSE；取最后一行（最新交易日）的 CLOSE（下标 4）
    latest = None
    for row in rows[1:]:
        if len(row) < 5 or not row[0].strip():
            continue
        close = row[4].strip()
        if not close:
            continue
        try:
            latest = (row[0].strip(), float(close))
        except ValueError:
            continue

    if not latest:
        return {"value": None, "date": None}
    return {"value": latest[1], "date": _parse_ymd(latest[0])}


async def _fetch_treasury(column: str) -> Dict[str, Any]:
    """抓取美国财政部当年每日收益率 CSV，取首行（最新日期）对应列的值。"""
    year = datetime.now().year
    url = TREASURY_URL.format(year=year)
    try:
        response = await _client.get(url)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"获取 Treasury收益率 {column} 失败: {e}", exc_info=True)
        return {"value": None, "date": None}

    rows = list(csv.reader(io.StringIO(response.text)))
    if not rows:
        return {"value": None, "date": None}

    header = [c.strip() for c in rows[0]]
    if column not in header:
        logger.error(f"Treasury CSV 未找到列 {column}, 表头: {header}")
        return {"value": None, "date": None}

    idx = header.index(column)
    # 数据第一行即最新交易日（按日期倒序）
    for row in rows[1:]:
        if len(row) <= idx:
            continue
        date_raw, val_raw = row[0].strip(), row[idx].strip()
        if not date_raw or not val_raw:
            continue
        try:
            return {"value": float(val_raw), "date": _parse_ymd(date_raw)}
        except ValueError:
            continue
    return {"value": None, "date": None}


async def _fetch_series(series: Dict[str, Any]) -> Dict[str, Any]:
    """按指标的 source 分发给对应的抓取函数。"""
    source = series["source"]
    if source == "fred":
        return await _fetch_fred(series["symbol"])
    if source == "cboe":
        return await _fetch_vix()
    if source == "treasury":
        return await _fetch_treasury(series["column"])
    logger.error(f"未知数据源 {source}，跳过指标 {series['name']}")
    return {"value": None, "date": None}


async def fetch_fred_prices() -> List[Dict[str, Any]]:
    """并发抓取全部指标，返回原始数据列表（不落库）。"""
    raws = await asyncio.gather(*(_fetch_series(s) for s in SERIES))
    items = []
    for series, raw in zip(SERIES, raws):
        items.append({
            "key": series["key"],
            "name": series["name"],
            "symbol": series["symbol"],
            "unit": series["unit"],
            "describe": series["describe"],
            "value": raw.get("value"),
            "date": raw.get("date"),
        })
    return items


def save_market_prices(items: List[Dict[str, Any]]) -> None:
    """将抓取到的指标按 (symbol, date) 写入/更新 market_prices 表（同步函数，供 asyncio.to_thread 调用）。

    去重逻辑：若库中该 symbol 的最新记录日期与本次抓取相同、且值也相同（如周末/非交易时间未更新），
    则跳过落库，仅当出现新日期或值发生变化时才写入。
    """
    db = SessionLocal()
    try:
        saved = 0
        for item in items:
            latest = (
                db.query(crud.models.MarketPrice)
                .filter(crud.models.MarketPrice.symbol == item["symbol"])
                .order_by(crud.models.MarketPrice.date.desc())
                .first()
            )
            # 数据未更新：最新记录日期相同且值相同 → 跳过，避免周末/非交易时间重复落库
            if latest and latest.date == item.get("date") and latest.value == item["value"]:
                logger.info(f"  - {item['name']}: 数据无更新({latest.date})，跳过")
                continue

            crud.upsert_market_price(db, schemas.MarketPriceCreate(
                symbol=item["symbol"],
                name=item["name"],
                unit=item["unit"],
                value=item["value"],
                date=item.get("date"),
            ))
            saved += 1
            # 落库成功后推进峰值回撤策略状态（新高上移 / 回撤达阈值重置）
            if item.get("value") is not None and item.get("date"):
                advance(db, item["symbol"], item["value"], item["date"])
        logger.info(f"市场行情数据已落库: {saved} 项指标 (跳过 {len(items) - saved} 项无更新)")
    finally:
        db.close()


async def refresh_market_data() -> Dict[str, Any]:
    """抓取全市场指标并落库（按 symbol+date 累积历史），返回原始数据供日志使用。"""
    items = await fetch_fred_prices()
    await asyncio.to_thread(save_market_prices, items)
    return {
        "items": items,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "FRED + CBOE + U.S. Treasury",
    }