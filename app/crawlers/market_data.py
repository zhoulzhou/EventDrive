#!/usr/bin/env python3
"""市场数据抓取模块（海外数据源：美国圣路易斯联储 FRED）。

服务器位于日本，FRED 接口在海外可正常访问且无需 API Key。
统一使用 FRED 获取纳斯达克指数、VIX、美债2年期/10年期收益率。

数据获取走「定时落库 + 页面读库」模式：
- 调度器调用 refresh_market_data() 抓取 FRED 并写入 market_prices 表；
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

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(timeout=15)

# FRED 数据源：按 Series ID 下载 CSV
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# 指标定义：key/unit 用于前端展示（收益率用 %）
SERIES = [
    {"key": "nasdaq", "name": "纳斯达克指数", "symbol": "NASDAQCOM", "unit": "", "describe": "NASDAQ Composite 综合指数"},
    {"key": "vix", "name": "VIX恐慌指数", "symbol": "VIXCLS", "unit": "", "describe": "CBOE Volatility Index"},
    {"key": "dgs2", "name": "美债2年期收益率", "symbol": "DGS2", "unit": "%", "describe": "2-Year Treasury Constant Maturity"},
    {"key": "dgs10", "name": "美债10年期收益率", "symbol": "DGS10", "unit": "%", "describe": "10-Year Treasury Constant Maturity"},
]


async def _fetch_series(symbol: str) -> Dict[str, Any]:
    """抓取单个 FRED 序列，返回最新值及其数据日期。"""
    try:
        response = await _client.get(FRED_URL, params={"id": symbol})
        response.raise_for_status()
    except Exception as e:
        logger.error(f"获取 FRED 序列 {symbol} 失败: {e}", exc_info=True)
        return {"value": None}

    rows = list(csv.reader(io.StringIO(response.text)))
    # 首行为表头 (observation_date,<symbol>)，跳过
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
        return {"value": None}

    date, value = values[-1]
    return {"value": value, "date": date}


async def fetch_fred_prices() -> List[Dict[str, Any]]:
    """并发抓取 FRED 全部指标，返回原始数据列表（不落库）。"""
    raws = await asyncio.gather(*(_fetch_series(s["symbol"]) for s in SERIES))
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
    """将抓取到的指标按 (symbol, date) 写入/更新 market_prices 表（同步函数，供 asyncio.to_thread 调用）。"""
    db = SessionLocal()
    try:
        for item in items:
            crud.upsert_market_price(db, schemas.MarketPriceCreate(
                symbol=item["symbol"],
                name=item["name"],
                unit=item["unit"],
                value=item["value"],
                date=item.get("date"),
            ))
        logger.info(f"市场行情数据已落库: {len(items)} 项指标")
    finally:
        db.close()


async def refresh_market_data() -> Dict[str, Any]:
    """抓取 FRED 全部指标并落库（按 symbol+date 累积历史），返回原始数据供日志使用。"""
    items = await fetch_fred_prices()
    await asyncio.to_thread(save_market_prices, items)
    return {
        "items": items,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "FRED (St. Louis Fed)",
    }