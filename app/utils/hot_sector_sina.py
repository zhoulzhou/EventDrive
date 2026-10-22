"""新浪财经概念板块数据源（今日热点页的**降级兜底**）。

只保留东财不可达时才会走到的路径。与东财源的差异：
- 板块与成分股是同一套分类（label 可直接查成分股），链路自洽；
- 但**不提供概念级资金流**（主力净流入），故降级时热度口径退化为「纯涨幅排序」；
- 板块层不提供涨跌家数与换手率，需由成分股聚合得到。

历史背景：早期东财 push2 对当时机房的出口 IP 返回 502，故改用新浪；
2026-10-21 实测东财已可直连（见 app/utils/hot_sector_em.py），新浪保留为兜底。
"""
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 伪板块（非真实题材）过滤：这些概念板块是统计口径聚合板块，不构成"热点题材"
_EXCLUDE_KEYWORDS = ("昨日", "连板", "融资融券", "转债", "机构重仓")


def _num(value) -> Optional[float]:
    """数值 → Python float；NaN/Inf/缺失一律返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(f) or f in (float("inf"), float("-inf")):
        return None
    return f


def _round(value, digits: int = 2) -> Optional[float]:
    f = _num(value)
    return None if f is None else round(f, digits)


def _clean_name(value) -> Optional[str]:
    """新浪会给出带填充空格的股票名（如「新 华 都」），去掉空格还原。"""
    if value is None:
        return None
    return str(value).replace(" ", "").strip() or None


def fetch_concept_boards() -> pd.DataFrame:
    """新浪概念板块行情：涨跌幅 / 公司家数 / 总成交额 / 领涨股。"""
    import akshare as ak

    return ak.stock_sector_spot(indicator="概念")


def fetch_board_stocks(label: str) -> pd.DataFrame:
    """新浪概念板块成分股行情。传 stock_sector_spot 返回的 label（如 gn_smgn）。

    与板块行情是同一套分类，label 可直接作为成分股接口的 node 参数，不需要名称映射。
    """
    import akshare as ak

    return ak.stock_sector_detail(sector=label)


def top_stocks(cons: pd.DataFrame, n: int) -> List[Dict[str, Any]]:
    """板块内按当日涨跌幅降序取前 n 只个股的交易情况（新浪成分股字段）。"""
    if cons is None or cons.empty:
        return []

    df = cons.copy()
    df["_涨跌幅"] = pd.to_numeric(df["changepercent"], errors="coerce")
    df = df[df["_涨跌幅"].notna()].nlargest(n, "_涨跌幅")

    stocks = []
    for _, row in df.iterrows():
        amount = _num(row.get("amount"))
        high, low, prev_close = _num(row.get("high")), _num(row.get("low")), _num(row.get("settlement"))
        # 新浪不直接给振幅，用（最高-最低）/昨收 换算
        amplitude = None
        if high is not None and low is not None and prev_close:
            amplitude = round((high - low) / prev_close * 100, 2)
        stocks.append({
            "code": str(row.get("code") or ""),
            "name": _clean_name(row.get("name")),
            "price": _round(row.get("trade")),
            "change_percent": _round(row.get("changepercent")),
            "amount": None if amount is None else round(amount / 1e8, 2),  # 元 → 亿元
            "turnover_rate": _round(row.get("turnoverratio")),
            "amplitude": amplitude,
            "pe": _round(row.get("per")),
        })
    return stocks
