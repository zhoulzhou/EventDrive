"""今日热点：A 股概念板块热度排名 + 驱动原因 + 板块内前五个股交易情况。

数据源（按优先级，首选失败自动降级）：
1. **东方财富**（首选，app/utils/hot_sector_em.py）—— 直连 push2 行情接口。
   概念板块全量一次拿齐：涨跌幅、**主力净流入**、主力净占比、上涨/下跌家数、
   换手率、总成交额、领涨股；成分股同理（最新价/涨跌幅/成交额/换手率/PE/振幅）。
   优势：板块层直接给「主力净流入」，可作为热度排序的第二维度；且不需聚合成分股
   才能得到涨跌家数与换手率。
2. **新浪财经**（降级兜底，akshare 封装）—— 板块与成分股同一套分类，但**无概念级资金流**，
   故降级时热度口径退化为「纯涨幅排序」，涨跌家数与换手率由成分股聚合。

热度口径：按**当日涨跌幅为主、主力资金净流入为辅**综合排序取前 N 个概念板块
（东财源启用资金维度；新浪降级源无资金流，退化为纯涨幅排序）。
量纲不可比（涨幅 % vs 净额 亿元），故各自排名后加权：涨幅排名 ×0.6 + 资金排名 ×0.4。

伪板块过滤：概念板块里混有大量统计口径板块（"昨日涨停""机构重仓""融资融券"
"深股通""预增预减""百元股""风格"等），它们不是题材，不构成"热点题材"，需剔除。

驱动原因：只用规则归纳（app.utils.hot_sector.rule_reason），由板块自身的客观数据拼装。
（曾接过 DeepSeek 归因，因输出无实质内容已移除，改为固定规则生成。）

本模块只负责「抓取 + 组装」，不读库也不缓存：
- 页面打开时的只读展示读的是库中快照（app/api/hot_sector.py 的 GET，不发外部请求）；
- 抓取只由用户点「刷新数据」（POST /api/hot-sector/refresh）或收盘后定时任务触发；
- 抓取结果的落库（按交易日写 hot_sector_snapshots 表，供复盘）抽在 app/utils/hot_sector_refresh.py，
  由页面接口与定时任务共用。
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from app.utils import hot_sector_em as em_source
from app.utils import hot_sector_sina as sina_source

logger = logging.getLogger(__name__)

# 板块/个股展示数量
TOP_N = 3
TOP_STOCKS = 5

# 热度排序权重：涨幅为主、主力净流入为辅（仅东财源可用）
WEIGHT_CHANGE = 0.6
WEIGHT_INFLOW = 0.4

# 伪板块（非真实题材）过滤。
#
# 东财 516 个概念板块里混着大量**统计口径板块**——它们按股东属性/指数成分/市值风格/
# 财报周期/技术位置划分，不是「今天的题材」，必须剔除，否则「昨日涨停」「机构重仓」
# 「百元股」这类会长期霸占热点榜。
#
# 规则用**子串黑名单**：按 2026-10-21 的实际 516 个板块名逐条校验，
# 命中 118 个统计口径板块，保留 400 个真实题材，无误伤（如「并购重组概念」
# 「专精特新」「券商概念」「首发经济」等题材均正确保留）。
_EXCLUDE_KEYWORDS = (
    # —— 昨日/板数类（连板情绪统计）——
    "昨日", "连板", "涨停", "首板", "多板", "触板", "炸板", "打二板",
    # —— 机构持股/资金属性类 ——
    "重仓", "增仓", "减仓", "新进", "持股", "养老金", "证金", "QFII",
    "大基金", "私募", "信托", "险资", "北向",
    # —— 指数成分类 ——
    "指数", "成分", "中证", "沪深", "上证", "深证", "深成", "创业",
    "科创板", "北交所", "MSCI", "富时", "标普", "标准普尔", "恒生",
    "AH股", "B股", "GDR", "做市",
    # —— 估值/市值/风格类 ——
    "破净", "破发", "破增发", "市净率", "百元股", "低价股", "微盘",
    "小盘", "中盘", "大盘", "成长", "价值", "周期", "红利", "权重",
    "ST股", "超跌", "趋势股", "高成长", "微利", "专精特新", "风格",
    # —— 业绩预告类（按财报周期聚合）——
    "季报", "中报", "年报", "预增", "预减", "预亏", "预盈", "扭亏", "首亏",
    # —— 交易属性类 ——
    "融资融券", "转债标的",
    # —— 技术位置类 ——
    "近期新高", "历史新高", "百日新高", "近期摘帽",
    # —— 东财自营统计口径类 ——
    "题材股", "热股", "东方财富", "密集调研", "股权激励", "股权分散",
    "股权集中", "举牌", "股权转让", "次新股", "高送转", "参股", "行业龙头",
    "IPO",
)


def _num(value) -> Optional[float]:
    """数值 → Python float；NaN/Inf/缺失一律返回 None，避免污染 JSON。"""
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


def _is_pseudo_board(name: str) -> bool:
    """判断是否为统计口径的伪板块（非真实题材）。"""
    if not name:
        return True
    return any(k in str(name) for k in _EXCLUDE_KEYWORDS)


# --------------------------------------------------------------------------- #
# 抓取（东财）
# --------------------------------------------------------------------------- #
def fetch_boards_em(deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    """东财概念板块全量行情（含主力净流入）。"""
    return em_source.fetch_concept_boards(deadline=deadline)


def fetch_board_stocks_em(board_code: str, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    """东财概念板块成分股行情。"""
    return em_source.fetch_board_stocks(board_code, deadline=deadline)


# --------------------------------------------------------------------------- #
# 抓取（新浪，降级兜底）
# --------------------------------------------------------------------------- #
def fetch_concept_boards_sina() -> pd.DataFrame:
    """新浪概念板块行情：涨跌幅 / 公司家数 / 总成交额 / 领涨股。"""
    return sina_source.fetch_concept_boards()


def fetch_board_stocks_sina(label: str) -> pd.DataFrame:
    """新浪概念板块成分股行情。"""
    return sina_source.fetch_board_stocks(label)


# --------------------------------------------------------------------------- #
# 交易日
# --------------------------------------------------------------------------- #
def resolve_trade_date() -> str:
    """最近一个 A 股交易日（YYYY-MM-DD），用于快照落库按交易日分组；取不到时退化为当日。

    复用股票指标模块已有的交易日历逻辑（akshare 新浪交易日历），避免各页各自维护；
    该函数内部已兜底，不会抛异常。
    """
    try:
        from app.utils.stock_temp import trading_days_ago

        days = trading_days_ago(1)
        if days:
            return days[-1]
    except Exception as e:
        logger.warning("获取 A 股交易日失败，快照日期退化为当日: %s", e)
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 排名与整理 —— 东财
# --------------------------------------------------------------------------- #
def pick_top_sectors_em(boards: List[Dict[str, Any]], n: int = TOP_N) -> List[Dict[str, Any]]:
    """按「涨幅为主、主力净流入为辅」综合排序取前 n 个东财概念板块。

    仅纳入当日上涨（涨跌幅 > 0）且非统计口径伪板块的概念。
    量纲归一：涨幅（%）与主力净流入（亿元）不可比，各自排名后加权。
    """
    rows = []
    for b in boards or []:
        name = b.get("f14")
        change = _num(b.get("f3"))
        if change is None or change <= 0:
            continue
        if _is_pseudo_board(name):
            continue
        rows.append({
            "name": name,
            "code": b.get("f12"),
            "change_percent": _round(change),
            "main_net_inflow": _round(_num(b.get("f62")) / 1e8) if _num(b.get("f62")) is not None else None,
            "main_net_inflow_ratio": _round(_num(b.get("f184"))),
            "turnover_rate": _round(_num(b.get("f8"))),
            "up_count": int(_num(b.get("f104")) or 0),
            "down_count": int(_num(b.get("f105")) or 0),
            "total_amount": _round(_num(b.get("f6")) / 1e8) if _num(b.get("f6")) is not None else None,
            "lead_stock": b.get("f128") or None,
            "lead_stock_change": _round(_num(b.get("f136"))),
        })

    if not rows:
        return []

    df = pd.DataFrame(rows)
    # 排名归一：名次越小得分越低，故用「名次 / 总数」后取负号做加权
    n_rows = len(df)
    df["_chg_rank"] = df["change_percent"].rank(ascending=False, method="min")
    inflow = pd.to_numeric(df["main_net_inflow"], errors="coerce")
    if inflow.notna().any():
        df["_flow_rank"] = inflow.fillna(inflow.min()).rank(ascending=False, method="min")
    else:
        df["_flow_rank"] = 1.0
    df["_score"] = (df["_chg_rank"] * WEIGHT_CHANGE + df["_flow_rank"] * WEIGHT_INFLOW) / n_rows

    picked = df.sort_values("_score").head(n)
    sectors = []
    for i, (_, row) in enumerate(picked.iterrows(), start=1):
        sectors.append({
            "rank": i,
            "name": row["name"],
            "code": row["code"],
            "change_percent": row["change_percent"],
            "main_net_inflow": row["main_net_inflow"],
            "main_net_inflow_ratio": row["main_net_inflow_ratio"],
            "turnover_rate": row["turnover_rate"],
            "up_count": int(row["up_count"]),
            "down_count": int(row["down_count"]),
            "total_amount": row["total_amount"],
            "lead_stock": row["lead_stock"],
            "lead_stock_change": row["lead_stock_change"],
            "stocks": [],
        })
    return sectors


def top_stocks_em(cons: List[Dict[str, Any]], n: int = TOP_STOCKS) -> List[Dict[str, Any]]:
    """东财成分股：按当日涨跌幅降序取前 n 只的交易情况。"""
    if not cons:
        return []

    rows = []
    for c in cons:
        pct = _num(c.get("f3"))
        if pct is None:
            continue
        rows.append(c)
    rows.sort(key=lambda c: _num(c.get("f3")) or 0, reverse=True)

    stocks = []
    for c in rows[:n]:
        price = _num(c.get("f2"))
        high, low, prev_close = _num(c.get("f15")), _num(c.get("f16")), _num(c.get("f18"))
        amount = _num(c.get("f6"))
        # 东财不直接给振幅，用（最高-最低）/昨收 换算
        amplitude = None
        if high is not None and low is not None and prev_close:
            amplitude = round((high - low) / prev_close * 100, 2)
        stocks.append({
            "code": str(c.get("f12") or ""),
            "name": c.get("f14") or None,
            "price": _round(price),
            "change_percent": _round(_num(c.get("f3"))),
            "amount": None if amount is None else round(amount / 1e8, 2),  # 元 → 亿元
            "turnover_rate": _round(_num(c.get("f8"))),
            "amplitude": amplitude,
            "pe": _round(_num(c.get("f9"))),
            "pb": _round(_num(c.get("f23"))),
        })
    return stocks


# --------------------------------------------------------------------------- #
# 排名与整理 —— 新浪（降级）
# --------------------------------------------------------------------------- #
def pick_top_sectors_sina(boards: pd.DataFrame, n: int = TOP_N) -> List[Dict[str, Any]]:
    """新浪降级路径：按当日涨跌幅降序取前 n 个概念板块（无资金流维度）。

    伪板块过滤与东财路径共用同一套黑名单（新浪概念板块里也有「昨日涨停」
    「融资融券」这类统计口径板块）。
    """
    if boards is None or boards.empty:
        return []

    df = boards.copy()
    df = df[pd.to_numeric(df["涨跌幅"], errors="coerce") > 0]
    if not df.empty:
        df = df[~df["板块"].astype(str).map(_is_pseudo_board)]
    if df.empty:
        return []

    picked = df.sort_values("涨跌幅", ascending=False).head(n)
    sectors = []
    for i, (_, row) in enumerate(picked.iterrows(), start=1):
        sectors.append({
            "rank": i,
            "name": row["板块"],
            "code": row["label"],
            "change_percent": _round(row["涨跌幅"]),
            "main_net_inflow": None,
            "main_net_inflow_ratio": None,
            "turnover_rate": None,      # 成分股聚合回填
            "up_count": 0,
            "down_count": 0,
            "total_amount": _round(_num(row.get("总成交额")) / 1e8) if _num(row.get("总成交额")) is not None else None,
            "lead_stock": sina_source._clean_name(row.get("股票名称")),
            "lead_stock_change": _round(row.get("个股-涨跌幅")),
            "stocks": [],
        })
    return sectors


def enrich_sina_with_stocks(sector: Dict[str, Any], cons: Optional[pd.DataFrame]) -> None:
    """新浪降级路径：把成分股写入 sector，并由成分股聚合涨跌家数与平均换手率。"""
    sector["stocks"] = sina_source.top_stocks(cons, TOP_STOCKS)
    if cons is None or cons.empty:
        sector["turnover_rate"] = None
        return

    pct = pd.to_numeric(cons.get("changepercent"), errors="coerce")
    sector["up_count"] = int((pct > 0).sum())
    sector["down_count"] = int((pct < 0).sum())

    turnover = pd.to_numeric(cons.get("turnoverratio"), errors="coerce").dropna()
    sector["turnover_rate"] = round(float(turnover.mean()), 2) if not turnover.empty else None


# --------------------------------------------------------------------------- #
# 驱动原因
# --------------------------------------------------------------------------- #
def rule_reason(sector: Dict[str, Any]) -> str:
    """用板块自身的客观数据拼装一句驱动说明（本页归因只走规则，不调用大模型）。"""
    parts = []
    if sector.get("change_percent") is not None:
        parts.append(f"板块今日上涨 {sector['change_percent']}%")
    if sector.get("main_net_inflow") is not None:
        parts.append(f"主力净流入 {sector['main_net_inflow']} 亿元")
    if sector.get("turnover_rate") is not None:
        parts.append(f"换手率 {sector['turnover_rate']}%")
    if sector.get("up_count") or sector.get("down_count"):
        parts.append(f"成分股 {sector.get('up_count', 0)} 涨 {sector.get('down_count', 0)} 跌")
    if sector.get("lead_stock"):
        lead = sector["lead_stock"]
        if sector.get("lead_stock_change") is not None:
            lead += f"（{sector['lead_stock_change']}%）"
        parts.append(f"领涨股 {lead}")
    if sector.get("stocks"):
        names = "、".join(s["name"] for s in sector["stocks"][:3] if s.get("name"))
        if names:
            parts.append(f"涨幅居前 {names}")
    return "；".join(parts) + "。" if parts else "暂无可用归因数据。"


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
async def _build_from_eastmoney(trade_date: str) -> Dict[str, Any]:
    """东财路径：抓板块 + 成分股，组装 payload。

    整轮抓取受 em_source._DEADLINE_SECONDS 总时限约束 —— 东财线路若「连得上但不回包」，
    每个请求都要耗满超时，翻 6 页会撞上沙箱反代 60 秒硬超时。
    超时即抛异常，由上层降级到新浪。
    """
    deadline = time.monotonic() + em_source._DEADLINE_SECONDS
    boards = await asyncio.to_thread(fetch_boards_em, deadline)
    sectors = pick_top_sectors_em(boards, TOP_N)
    if not sectors:
        return {"status": "error",
                "message": "未获取到上涨的概念板块数据（可能为非交易时段或数据源异常）",
                "source": "东方财富"}

    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_board_stocks_em, s["code"], deadline) for s in sectors),
        return_exceptions=True,
    )
    errors: List[str] = []
    for sector, cons in zip(sectors, results):
        if isinstance(cons, Exception):
            logger.error("抓取板块 %s 成分股失败: %s", sector["name"], cons, exc_info=cons)
            errors.append(f"{sector['name']}: 成分股获取失败")
            sector["stocks"] = []
            continue
        sector["stocks"] = top_stocks_em(cons, TOP_STOCKS)

    for sector in sectors:
        sector["reason"] = rule_reason(sector)

    return {
        "status": "ok",
        "trade_date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "东方财富 · 概念板块行情",
        "source_key": "eastmoney",
        "sectors": sectors,
        "errors": errors,
    }


async def _build_from_sina(trade_date: str) -> Dict[str, Any]:
    """新浪降级路径：抓板块 + 成分股，组装 payload。"""
    boards = await asyncio.to_thread(fetch_concept_boards_sina)
    sectors = pick_top_sectors_sina(boards, TOP_N)
    if not sectors:
        return {"status": "error",
                "message": "未获取到上涨的概念板块数据（可能为非交易时段或数据源异常）",
                "source": "新浪财经"}

    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_board_stocks_sina, s["code"]) for s in sectors),
        return_exceptions=True,
    )
    errors: List[str] = []
    for sector, cons in zip(sectors, results):
        if isinstance(cons, Exception):
            logger.error("抓取板块 %s 成分股失败: %s", sector["name"], cons, exc_info=cons)
            errors.append(f"{sector['name']}: 成分股获取失败")
            enrich_sina_with_stocks(sector, None)
            continue
        enrich_sina_with_stocks(sector, cons)

    for sector in sectors:
        sector["reason"] = rule_reason(sector)

    return {
        "status": "ok",
        "trade_date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "新浪财经 · 概念板块行情（东财不可达，已降级）",
        "source_key": "sina",
        "sectors": sectors,
        "errors": errors,
    }


async def build_hot_sector_payload() -> Dict[str, Any]:
    """抓取并组装今日热点数据（每次调用都真实抓取，不做缓存）。

    **数据源优先级：东方财富 → 新浪财经（降级）**。
    东财不可达（网络/风控）时自动降级到新浪，并在 payload 里标明实际使用的源，
    保证页面始终有数据。降级路径无概念级资金流，热度口径退化为纯涨幅排序。
    """
    trade_date = await asyncio.to_thread(resolve_trade_date)

    # ---- 首选：东方财富 ----
    try:
        return await _build_from_eastmoney(trade_date)
    except Exception as e:
        logger.warning("东方财富数据源不可用，降级到新浪财经: %s", e, exc_info=e)

    # ---- 降级：新浪财经 ----
    try:
        payload = await _build_from_sina(trade_date)
        payload.setdefault("errors", []).append("东方财富数据源不可达，已降级使用新浪财经数据")
        return payload
    except Exception as e:
        logger.error("新浪财经数据源也不可用: %s", e, exc_info=e)
        return {
            "status": "error",
            "message": "获取概念板块行情失败：东方财富与新浪财经数据源均不可达。"
                       "请检查服务器网络/代理，或稍后重试；抓取详情见服务端日志。",
            "detail": str(e),
        }
