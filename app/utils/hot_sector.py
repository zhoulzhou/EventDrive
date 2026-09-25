"""今日热点：A 股概念板块热度排名 + 驱动原因 + 板块内前五个股交易情况。

数据源（均为 akshare 封装的东方财富公开接口，无需 Key）：
- ak.stock_board_concept_name_em()          概念板块实时行情（涨跌幅 / 换手率 / 上涨下跌家数 / 领涨股）
- ak.stock_sector_fund_flow_rank()          概念板块当日资金流排名（主力净流入净额）
- ak.stock_board_concept_cons_em(code)      板块成分股实时行情（个股涨跌幅 / 成交额 / 换手率）

热度口径（产品要求）：以**当日涨跌幅为主、主力资金净流入为辅**综合排序，取前 3 个概念板块。
综合评分 = 涨幅排名 × 0.6 + 资金净流入排名 × 0.4（排名越靠前分数越小）。

驱动原因：调用大模型 DeepSeek 结合板块数据与近期新闻标题归因；
未配置 DEEPSEEK_API_KEY 或调用失败时，降级为基于板块数据的规则归纳，保证页面始终可用。

本模块只负责「抓取 + 组装」，不读库也不缓存：

- 页面打开时的只读展示读的是库中快照（app/api/hot_sector.py 的 GET，不发外部请求）；
- 抓取只由用户点「刷新盘面」（POST /api/hot-sector/refresh）或收盘后定时任务触发；
- 抓取结果的落库（按交易日写 hot_sector_snapshots 表，供复盘）抽在 app/utils/hot_sector_refresh.py，
  由页面接口与定时任务共用。
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)

# 板块/个股展示数量
TOP_N = 3
TOP_STOCKS = 5

# 伪板块（非真实题材）过滤：这些概念板块是统计口径聚合板块，不构成"热点题材"
_EXCLUDE_KEYWORDS = ("昨日", "连板", "融资融券", "转债", "机构重仓")

_client = httpx.AsyncClient(timeout=60, trust_env=False)


def _num(value) -> Optional[float]:
    """pandas 数值 → Python float；NaN/Inf/缺失一律返回 None，避免污染 JSON。"""
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


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def fetch_concept_boards() -> pd.DataFrame:
    """概念板块实时行情：涨跌幅 / 换手率 / 上涨下跌家数 / 领涨股。"""
    import akshare as ak

    return ak.stock_board_concept_name_em()


def fetch_concept_fund_flow() -> pd.DataFrame:
    """概念板块当日资金流排名：主力净流入净额（元）。"""
    import akshare as ak

    return ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流")


def fetch_board_stocks(code: str) -> pd.DataFrame:
    """板块成分股实时行情。传板块代码（BKxxxx）可省去 akshare 内部一次名称映射请求。"""
    import akshare as ak

    return ak.stock_board_concept_cons_em(symbol=code)


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
# 排名与整理
# --------------------------------------------------------------------------- #
def pick_top_sectors(boards: pd.DataFrame, flows: pd.DataFrame, n: int = TOP_N) -> List[Dict[str, Any]]:
    """按「涨幅为主、资金净流入为辅」综合排序，返回前 n 个概念板块。

    仅纳入当日上涨（涨跌幅 > 0）且非伪板块的概念，避免"昨日涨停"这类统计板块占据榜首。
    """
    if boards is None or boards.empty:
        return []

    df = boards.copy()
    df = df[pd.to_numeric(df["涨跌幅"], errors="coerce") > 0]
    if not df.empty:
        mask = ~df["板块名称"].astype(str).str.contains("|".join(_EXCLUDE_KEYWORDS), na=False)
        df = df[mask]
    if df.empty:
        return []

    inflow_col = "今日主力净流入-净额"
    if flows is not None and not flows.empty and inflow_col in flows.columns:
        df = df.merge(
            flows[["名称", inflow_col]], left_on="板块名称", right_on="名称", how="left"
        )
    else:
        df[inflow_col] = None

    # 排名法做量纲归一：涨幅与资金净流入绝对值不可比，先各自排名再加权
    df["_涨幅排名"] = df["涨跌幅"].rank(ascending=False, method="min")
    inflow = pd.to_numeric(df[inflow_col], errors="coerce")
    df["_资金排名"] = inflow.fillna(inflow.min() - 1 if inflow.notna().any() else 0).rank(
        ascending=False, method="min"
    )
    df["_评分"] = df["_涨幅排名"] * 0.6 + df["_资金排名"] * 0.4

    picked = df.nsmallest(n, "_评分")
    sectors = []
    for i, (_, row) in enumerate(picked.iterrows(), start=1):
        inflow_val = _num(row.get(inflow_col))
        sectors.append({
            "rank": i,
            "name": row["板块名称"],
            "code": row["板块代码"],
            "change_percent": _round(row["涨跌幅"]),
            "main_net_inflow": None if inflow_val is None else round(inflow_val / 1e8, 2),  # 元 → 亿元
            "turnover_rate": _round(row.get("换手率")),
            "up_count": int(_num(row.get("上涨家数")) or 0),
            "down_count": int(_num(row.get("下跌家数")) or 0),
            "lead_stock": row.get("领涨股票"),
            "lead_stock_change": _round(row.get("领涨股票-涨跌幅")),
        })
    return sectors


def top_stocks(cons: pd.DataFrame, n: int = TOP_STOCKS) -> List[Dict[str, Any]]:
    """板块内按当日涨跌幅降序取前 n 只个股的交易情况。"""
    if cons is None or cons.empty:
        return []

    df = cons.copy()
    df["_涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
    df = df[df["_涨跌幅"].notna()].nlargest(n, "_涨跌幅")

    stocks = []
    for _, row in df.iterrows():
        amount = _num(row.get("成交额"))
        stocks.append({
            "code": str(row.get("代码") or ""),
            "name": row.get("名称"),
            "price": _round(row.get("最新价")),
            "change_percent": _round(row.get("涨跌幅")),
            "amount": None if amount is None else round(amount / 1e8, 2),  # 元 → 亿元
            "turnover_rate": _round(row.get("换手率")),
            "amplitude": _round(row.get("振幅")),
            "pe": _round(row.get("市盈率-动态")),
        })
    return stocks


# --------------------------------------------------------------------------- #
# 驱动原因
# --------------------------------------------------------------------------- #
def rule_reason(sector: Dict[str, Any]) -> str:
    """规则归纳（无大模型时的兜底）：用板块自身的客观数据拼装一句驱动说明。"""
    parts = []
    if sector.get("change_percent") is not None:
        parts.append(f"板块今日上涨 {sector['change_percent']}%")
    if sector.get("main_net_inflow") is not None:
        flow = sector["main_net_inflow"]
        parts.append(f"主力净{'流入' if flow >= 0 else '流出'} {abs(flow)} 亿元")
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


def _llm_config() -> Optional[Dict[str, str]]:
    """驱动原因固定走 DeepSeek（OpenAI 兼容 chat/completions 协议）。"""
    if settings.DEEPSEEK_API_KEY:
        return {
            "label": "DeepSeek",
            "url": "https://api.deepseek.com/v1/chat/completions",
            "key": settings.DEEPSEEK_API_KEY,
            "model": settings.DEEPSEEK_MODEL,
        }
    return None


def _build_prompt(sectors: List[Dict[str, Any]], news_titles: List[str]) -> str:
    lines = []
    for s in sectors:
        lines.append(
            f"{s['rank']}. {s['name']} | 涨跌幅 {s['change_percent']}% | "
            f"主力净流入 {s['main_net_inflow']} 亿 | {s['up_count']}涨{s['down_count']}跌 | "
            f"换手率 {s['turnover_rate']}% | 领涨股 {s['lead_stock']}（{s['lead_stock_change']}%）"
        )
        names = "、".join(
            f"{st['name']}({st['change_percent']}%)" for st in s.get("stocks", []) if st.get("name")
        )
        if names:
            lines.append(f"   板块内涨幅前五：{names}")
    board_block = "\n".join(lines)

    news_block = "\n".join(f"- {t}" for t in news_titles[:40]) or "（近期无可用新闻标题）"

    return f"""以下是今日 A 股涨幅居前且资金净流入的概念板块（已按综合热度排序）及板块内领涨个股：

【板块数据】
{board_block}

【最近 48 小时财经新闻标题】
{news_block}

请为上述每个板块给出 1 段「驱动原因」，说明是什么事件、政策、产业逻辑或资金动向推动其上涨。

要求：
1. 每个板块只输出一段话，60 字以内，具体、可验证，不要套话和免责声明。
2. 优先结合上面的新闻标题；若新闻与板块无直接关系，则依据板块数据与领涨个股特征做合理归因。
3. 不要编造具体的政策名称、事件或数据；无法判断时，直接描述资金与个股表现特征。
4. 严格输出 JSON，不要任何多余文字：{{"reasons":[{{"name":"板块名","reason":"驱动原因"}}]}}"""


def _extract_json(text: str) -> Optional[dict]:
    """从模型输出中提取 JSON（兼容 ```json 代码块与前后多余文字）。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    brace = re.search(r"\{.*\}", cleaned, re.S)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def generate_reasons(
    sectors: List[Dict[str, Any]], news_titles: List[str]
) -> Dict[str, Any]:
    """生成各板块驱动原因。

    返回 {"reasons": {板块名: 原因}, "source": "llm"/"rule", "model": str|None}。
    无可用大模型或调用/解析失败时，整体降级为规则归纳。
    """
    fallback = {
        "reasons": {s["name"]: rule_reason(s) for s in sectors},
        "source": "rule",
        "model": None,
    }
    cfg = _llm_config()
    if not cfg or not sectors:
        if not cfg:
            logger.info("未配置 DeepSeek API Key，热点驱动原因降级为规则归纳")
        return fallback

    try:
        resp = await _client.post(
            cfg["url"],
            headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [
                    {"role": "system", "content": "你是 A 股短线热点分析师，只输出 JSON。"},
                    {"role": "user", "content": _build_prompt(sectors, news_titles)},
                ],
                "temperature": 0.4,
                "stream": False,
            },
        )
        if resp.status_code != 200:
            logger.error("%s 生成热点归因失败 %s: %s", cfg["label"], resp.status_code, resp.text[:200])
            return fallback

        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if not parsed or not isinstance(parsed.get("reasons"), list):
            logger.error("%s 热点归因返回结构异常: %s", cfg["label"], str(content)[:200])
            return fallback

        reasons = {}
        for item in parsed["reasons"]:
            name = str(item.get("name", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if name and reason:
                reasons[name] = reason

        # 模型漏答的板块用规则归纳补上，避免页面出现空白
        for s in sectors:
            reasons.setdefault(s["name"], rule_reason(s))
        return {"reasons": reasons, "source": "llm", "model": f"{cfg['label']} / {cfg['model']}"}
    except Exception as e:
        logger.error("生成热点归因异常: %s", e, exc_info=True)
        return fallback


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
async def build_hot_sector_payload(
    news_titles: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """抓取并组装今日热点数据（每次调用都真实抓取，不做缓存）。"""
    errors: List[str] = []
    try:
        boards, flows, trade_date = await asyncio.gather(
            asyncio.to_thread(fetch_concept_boards),
            asyncio.to_thread(fetch_concept_fund_flow),
            asyncio.to_thread(resolve_trade_date),
        )
    except Exception as e:
        logger.error("抓取概念板块数据失败: %s", e, exc_info=True)
        return {"status": "error", "message": f"抓取概念板块数据失败：{e}"}

    sectors = pick_top_sectors(boards, flows, TOP_N)
    if not sectors:
        return {"status": "error", "message": "未获取到上涨的概念板块数据（可能为非交易时段或数据源异常）"}

    # 三个板块的成分股并发抓取，单个板块失败不影响其余板块
    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_board_stocks, s["code"]) for s in sectors),
        return_exceptions=True,
    )
    for sector, cons in zip(sectors, results):
        if isinstance(cons, Exception):
            logger.error("抓取板块 %s 成分股失败: %s", sector["name"], cons, exc_info=cons)
            errors.append(f"{sector['name']}: 成分股获取失败")
            sector["stocks"] = []
            continue
        sector["stocks"] = top_stocks(cons, TOP_STOCKS)

    reason_result = await generate_reasons(sectors, news_titles or [])
    for sector in sectors:
        sector["reason"] = reason_result["reasons"].get(sector["name"], rule_reason(sector))

    payload = {
        "status": "ok",
        "trade_date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "东方财富 · akshare 实时行情",
        "reason_source": reason_result["source"],
        "reason_model": reason_result["model"],
        "sectors": sectors,
        "errors": errors,
    }
    return payload