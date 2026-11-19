"""主题资金（/theme-capital）：算力产业链与人形机器人产业链的每日快照与归因。

设计要点
--------
页面定位是「这两条产业链今天发生了什么、为什么」。数据每天收盘轮生成一次、落一张
快照表，页面只读快照 —— 与 daily_hotspots 同一套路，避免每次开页打十几个外部接口。

**为什么用「显式成分股篮子」而不是概念板块的净流入**
    · 东财 ``clist/get``（板块/个股资金流榜）在云沙箱里被路径级拦截（见 hotspot.py
      注释），而 ``ulist.np/get`` 正常 —— 按 secid 直接查指定个股的资金流最稳。
    · 「算力产业链」「人形机器人产业链」在东财里不是单一板块，分散在光模块、AI 芯片、
      服务器、减速器、丝杠等多个二级行业里。显式维护两条篮子（带细分环节标签），
      才能回答「这条链里谁在吸金、谁在失血」，而不是笼统一个板块数字。
    · 板块资金流历史（fflow/daykline 的 90.BKxxxx）实测取不到，所以「净流入趋势」用
      成分股各自的历史主力净额（fflow/daykline，push2his 域名可用）按同法求和 ——
      篮子固定，跨日可比。

**请求清单**（都在后台线程跑，不占反代 60s）
    1. ulist.np/get × ceil(成分股/每批)  —— 当日现价/涨跌/主力净额（一次可查多只）
    2. push2ex getTopicZTPool × 1      —— 涨停池，按成分股代码筛出这两条链的涨停龙头
    3. fflow/daykline × 成分股数        —— 近 N 日主力净额历史（趋势曲线）
    4. 库内 news（当日）+ 近几日快照    —— 相关新闻时间轴 / LLM 跨日档案

**口径**（页面必须写明）
    · 主力净额 = 东财 f62（超大单+大单净额），单位元，展示时折成亿元。
    · 产业链净流入 = 篮内成分股主力净额之和；成分股是人工维护的代表股，不是全市场，
      所以是「观察样本」而非官方行业统计。涨跌/资金为收盘快照，非实时。
    · 涨停龙头来自涨停池当日结果，仅统计篮内个股。

**失败降级**：任何一路抓取失败都记进 data_gaps，页面上明确提示缺哪块，绝不静默填空。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from app import crud
from app.utils.anti_crawl import get_random_headers

logger = logging.getLogger(__name__)


class ThemeCapitalError(RuntimeError):
    """主题资金抓取失败（区别于「抓到但为空」）。"""


# ================================================================ 成分股篮子
# (secid, 展示名, 细分环节)。secid 前缀：0=深市, 1=沪市。
# 名称以 ulist.np 返回为准，这里存的是校验过的期望名（接口偶发缺字段时兜底）。
# 更新篮子时：改完跑一遍 .tmp 里的校验，确保 secid 对得上名字。
COMPUTE_CHAIN: Tuple[Dict[str, str], ...] = (
    # 光模块 / CPO
    {"secid": "0.300308", "name": "中际旭创", "seg": "光模块/CPO"},
    {"secid": "0.300502", "name": "新易盛", "seg": "光模块/CPO"},
    {"secid": "0.300394", "name": "天孚通信", "seg": "光模块/CPO"},
    {"secid": "0.002281", "name": "光迅科技", "seg": "光模块/CPO"},
    {"secid": "0.300570", "name": "太辰光", "seg": "光模块/CPO"},
    # AI 芯片
    {"secid": "1.688256", "name": "寒武纪", "seg": "AI芯片"},
    {"secid": "1.688041", "name": "海光信息", "seg": "AI芯片"},
    {"secid": "1.688008", "name": "澜起科技", "seg": "AI芯片"},
    {"secid": "0.300474", "name": "景嘉微", "seg": "AI芯片"},
    # 服务器 / 整机
    {"secid": "1.601138", "name": "工业富联", "seg": "服务器/整机"},
    {"secid": "0.000977", "name": "浪潮信息", "seg": "服务器/整机"},
    {"secid": "1.603019", "name": "中科曙光", "seg": "服务器/整机"},
    {"secid": "0.000938", "name": "紫光股份", "seg": "服务器/整机"},
    # PCB / 覆铜板
    {"secid": "0.002463", "name": "沪电股份", "seg": "PCB/覆铜板"},
    {"secid": "0.300476", "name": "胜宏科技", "seg": "PCB/覆铜板"},
    {"secid": "1.600183", "name": "生益科技", "seg": "PCB/覆铜板"},
    # 液冷 / 温控
    {"secid": "0.002837", "name": "英维克", "seg": "液冷/温控"},
    {"secid": "0.301018", "name": "申菱环境", "seg": "液冷/温控"},
    # 数据中心 / IDC
    {"secid": "0.300442", "name": "润泽科技", "seg": "数据中心/IDC"},
    {"secid": "0.300383", "name": "光环新网", "seg": "数据中心/IDC"},
    {"secid": "0.300738", "name": "奥飞数据", "seg": "数据中心/IDC"},
    # 铜连接
    {"secid": "0.002130", "name": "沃尔核材", "seg": "铜连接"},
    # 存储
    {"secid": "1.603986", "name": "兆易创新", "seg": "存储"},
    {"secid": "0.301308", "name": "江波龙", "seg": "存储"},
    # 电源
    {"secid": "0.002851", "name": "麦格米特", "seg": "电源"},
)

ROBOT_CHAIN: Tuple[Dict[str, str], ...] = (
    # 执行器 / 总成
    {"secid": "0.002050", "name": "三花智控", "seg": "执行器/总成"},
    {"secid": "1.601689", "name": "拓普集团", "seg": "执行器/总成"},
    # 减速器
    {"secid": "1.688017", "name": "绿的谐波", "seg": "减速器"},
    {"secid": "0.002472", "name": "双环传动", "seg": "减速器"},
    {"secid": "0.002896", "name": "中大力德", "seg": "减速器"},
    {"secid": "1.603915", "name": "国茂股份", "seg": "减速器"},
    # 电机
    {"secid": "1.603728", "name": "鸣志电器", "seg": "电机"},
    {"secid": "0.300660", "name": "江苏雷利", "seg": "电机"},
    {"secid": "1.688160", "name": "步科股份", "seg": "电机"},
    {"secid": "1.688320", "name": "禾川科技", "seg": "电机"},
    {"secid": "1.688698", "name": "伟创电气", "seg": "电机"},
    # 丝杠
    {"secid": "0.300580", "name": "贝斯特", "seg": "丝杠"},
    {"secid": "1.603667", "name": "五洲新春", "seg": "丝杠"},
    {"secid": "1.601100", "name": "恒立液压", "seg": "丝杠"},
    # 传感器
    {"secid": "1.603662", "name": "柯力传感", "seg": "传感器"},
    {"secid": "1.688322", "name": "奥比中光", "seg": "传感器"},
    {"secid": "0.300007", "name": "汉威科技", "seg": "传感器"},
    # 灵巧手 / 结构件
    {"secid": "0.003021", "name": "兆威机电", "seg": "灵巧手/结构件"},
    {"secid": "1.600114", "name": "东睦股份", "seg": "灵巧手/结构件"},
    # 伺服 / 控制
    {"secid": "0.300124", "name": "汇川技术", "seg": "伺服/控制"},
    {"secid": "1.603416", "name": "信捷电气", "seg": "伺服/控制"},
    # 轻量化材料
    {"secid": "0.002915", "name": "中欣氟材", "seg": "轻量化材料"},
    {"secid": "0.301076", "name": "新瀚新材", "seg": "轻量化材料"},
    # 本体
    {"secid": "0.002747", "name": "埃斯顿", "seg": "机器人本体"},
    {"secid": "0.300024", "name": "机器人", "seg": "机器人本体"},
)

# 链元信息：key -> 定义。顺序即页面展示顺序。
CHAINS: Tuple[Tuple[str, str, str, Tuple[Dict[str, str], ...], Tuple[str, ...]], ...] = (
    ("compute", "算力产业链", "光模块·AI芯片·服务器·PCB·液冷·IDC·存储",
     COMPUTE_CHAIN, ("算力", "光模块", "CPO", "AI芯片", "英伟达", "数据中心", "液冷",
                     "服务器", "存储芯片", "铜缆", "PCB", "智算", "GPU")),
    ("robot", "人形机器人产业链", "执行器·减速器·电机·丝杠·传感器·灵巧手·本体",
     ROBOT_CHAIN, ("人形机器人", "机器人", "减速器", "丝杠", "灵巧手", "伺服", "电机",
                   "执行器", "特斯拉", "Optimus", "传感器", "空心杯", "PEEK")),
)

# 新闻关键词命中时的分类标签
_NEWS_TAG_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("政策", ("政策", "工信部", "发改委", "国务院", "规划", "补贴", "专项")),
    ("产业", ("发布", "量产", "投产", "签约", "订单", "中标", "扩产", "新品", "突破")),
    ("资本", ("融资", "IPO", "上市", "收购", "增持", "减持", "定增", "市值")),
    ("海外", ("特斯拉", "英伟达", "Optimus", "海外", "美股", "马斯克")),
)

# ================================================================ HTTP 抓取
EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EM_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_REFERER = "https://quote.eastmoney.com/"

_ULIST_BATCH = 12        # 单批 secid 数（URL 太长会被 WAF 直接掐断）
_REQ_GAP = 0.35          # 请求间隔（秒）
_TIMEOUT = 10
_RETRIES = 3
_HISTORY_DAYS = 60       # 趋势曲线保留的交易日数

_session: Optional[requests.Session] = None


def _http() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        _session = s
    return _session


def _fetch_json(url: str, params: Dict[str, Any], what: str, *,
                retries: int = _RETRIES, expect_list: bool = False,
                referer: str = EM_REFERER):
    last = "未知错误"
    for attempt in range(retries):
        try:
            resp = _http().get(url, params=params, timeout=_TIMEOUT,
                               headers=get_random_headers(referer))
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) or (expect_list and isinstance(data, list)):
                    return data
                last = "响应结构不是预期 JSON"
            else:
                last = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 —— 连接类异常全兜
            last = f"{type(exc).__name__}: {str(exc)[:90]}"
        if attempt < retries - 1:
            time.sleep(1.2 * (attempt + 1))
    raise ThemeCapitalError(f"{what} 抓取失败（{retries} 次）：{last}")


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "-":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _yi(value: Optional[float]) -> Optional[float]:
    """元 → 亿元，保留两位。"""
    return round(value / 1e8, 2) if value is not None else None


def _news_tag(title: str) -> str:
    for tag, words in _NEWS_TAG_RULES:
        if any(w in title for w in words):
            return tag
    return "产业"


def _all_secids() -> List[str]:
    seen: List[str] = []
    for _, _, _, stocks, _ in CHAINS:
        for s in stocks:
            if s["secid"] not in seen:
                seen.append(s["secid"])
    return seen


def fetch_quotes() -> Dict[str, Dict[str, Any]]:
    """批量取成分股当日现价 / 涨跌幅 / 主力净额。返回 secid -> row。

    ulist.np 一次可查多只，但 URL 过长会被 WAF 掐断，所以按 _ULIST_BATCH 分批。
    字段：f2 现价 / f3 涨跌幅 / f12 代码 / f14 名称 / f62 主力净额(元) / f184 主力净占比。
    """
    secids = _all_secids()
    out: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for i in range(0, len(secids), _ULIST_BATCH):
        batch = secids[i:i + _ULIST_BATCH]
        try:
            data = _fetch_json(EM_ULIST_URL, {
                "fltt": "2", "invt": "2",
                "fields": "f2,f3,f12,f14,f62,f184",
                "secids": ",".join(batch),
            }, what="成分股行情")
            diff = ((data.get("data") or {}).get("diff")) or []
            for row in diff:
                code = str(row.get("f12") or "")
                # f12 只有 6 位代码，回查属于哪个 secid
                sid = next((s for s in batch if s.endswith("." + code)), None)
                if not sid:
                    continue
                out[sid] = {
                    "code": code,
                    "name": row.get("f14") or "",
                    "price": _f(row.get("f2")),
                    "change_pct": _f(row.get("f3")),
                    "net_inflow": _f(row.get("f62")),
                    "net_ratio": _f(row.get("f184")),
                }
        except ThemeCapitalError as exc:
            errors.append(str(exc))
        time.sleep(_REQ_GAP)
    if not out:
        raise ThemeCapitalError("成分股行情全部抓取失败：" + (errors[-1] if errors else "空响应"))
    return out


def fetch_zt_pool(date_str: str) -> List[Dict[str, Any]]:
    """抓涨停池，返回原始条目（供按成分股代码筛龙头）。date_str = YYYYMMDD。"""
    data = _fetch_json(EM_ZT_URL, {
        "ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "600",
        "sort": "fbt:asc", "date": date_str,
    }, what="涨停池")
    body = data.get("data") or {}
    return body.get("pool") or []


def fetch_history(secid: str, *, days: int = _HISTORY_DAYS) -> Dict[str, float]:
    """取单只成分股近 N 日主力净额，返回 {YYYY-MM-DD: 元}。单只失败返回空，不抛。"""
    try:
        data = _fetch_json(EM_FFLOW_URL, {
            "lmt": str(days), "klt": "101", "secid": secid,
            "fields1": "f1,f2,f3,f7", "fields2": "f51,f52",
        }, what=f"资金历史 {secid}", retries=2)
    except ThemeCapitalError as exc:
        logger.warning("主题资金：%s 历史抓取失败 %s", secid, exc)
        return {}
    klines = ((data.get("data") or {}).get("klines")) or []
    out: Dict[str, float] = {}
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        val = _f(parts[1])
        if val is not None:
            out[parts[0]] = val
    return out


# ================================================================ 聚合
def _chain_snapshot(key: str, name: str, subtitle: str, stocks: Tuple[Dict[str, str], ...],
                    keywords: Tuple[str, ...], quotes: Dict[str, Dict[str, Any]],
                    zt_items: List[Dict[str, Any]],
                    news_rows: List[Any]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for s in stocks:
        q = quotes.get(s["secid"]) or {}
        rows.append({
            "secid": s["secid"],
            "code": q.get("code") or s["secid"].split(".", 1)[1],
            "name": q.get("name") or s["name"],
            "seg": s["seg"],
            "price": q.get("price"),
            "change_pct": q.get("change_pct"),
            "net_inflow_yi": _yi(q.get("net_inflow")),
            "net_ratio": q.get("net_ratio"),
        })
    rows = [r for r in rows if r["net_inflow_yi"] is not None]

    total_yi = round(sum(r["net_inflow_yi"] or 0 for r in rows), 2)
    valid_chg = [r["change_pct"] for r in rows if r["change_pct"] is not None]
    avg_chg = round(sum(valid_chg) / len(valid_chg), 2) if valid_chg else None
    up = sum(1 for c in valid_chg if c > 0)
    down = sum(1 for c in valid_chg if c < 0)

    by_inflow = sorted(rows, key=lambda r: (r["net_inflow_yi"] or 0), reverse=True)
    # 涨停龙头：涨停池条目按代码命中篮内
    code_seg = {r["code"]: r["seg"] for r in rows}
    code_name = {r["code"]: r["name"] for r in rows}
    leaders: List[Dict[str, Any]] = []
    for it in zt_items:
        code = str(it.get("c") or "")
        if code not in code_seg:
            continue
        try:
            board = int(it.get("lbc") or 1)
        except (TypeError, ValueError):
            board = 1
        leaders.append({
            "code": code,
            "name": code_name.get(code) or it.get("n") or "",
            "seg": code_seg[code],
            "board": board,
            "seal_fund_yi": _yi(_f(it.get("fund"))),
            "first_seal": _fmt_time(it.get("fbt")),
            "broken_times": int(it.get("zbc") or 0),
        })
    leaders.sort(key=lambda x: (-x["board"], -(x["seal_fund_yi"] or 0)))

    news: List[Dict[str, Any]] = []
    for row in news_rows:
        title = str(_get(row, "title") or "")
        if not title:
            continue
        hit = next((k for k in keywords if k in title), None)
        if not hit:
            continue
        pub = _get(row, "publish_time")
        news.append({
            "title": title,
            "url": _get(row, "url") or "",
            "time": pub.strftime("%m-%d %H:%M") if hasattr(pub, "strftime") else "",
            "tag": _news_tag(title),
            "hit": hit,
        })
    news.sort(key=lambda x: -_sort_key(x["time"]))
    seen_title = set()
    news_picked = []
    for n in news:
        if n["title"] in seen_title:
            continue
        seen_title.add(n["title"])
        news_picked.append(n)
        if len(news_picked) >= 12:
            break

    return {
        "key": key,
        "name": name,
        "subtitle": subtitle,
        "stock_count": len(rows),
        "net_inflow_yi": total_yi,
        "avg_change_pct": avg_chg,
        "up_count": up,
        "down_count": down,
        "by_seg": _by_segment(rows),
        "inflow_top": by_inflow[:8],
        "outflow_top": list(reversed(by_inflow[-8:])),
        "all_stocks": rows,
        "leaders": leaders[:12],
        "news": news_picked,
    }


def _by_segment(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按细分环节聚合净流入，回答「链条里哪一段在吸金」。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seg = r["seg"]
        b = buckets.setdefault(seg, {"seg": seg, "net_inflow_yi": 0.0, "count": 0,
                                     "changes": []})
        b["net_inflow_yi"] += r["net_inflow_yi"] or 0
        b["count"] += 1
        if r["change_pct"] is not None:
            b["changes"].append(r["change_pct"])
    out = []
    for b in buckets.values():
        chg = b["changes"]
        out.append({
            "seg": b["seg"],
            "net_inflow_yi": round(b["net_inflow_yi"], 2),
            "avg_change_pct": round(sum(chg) / len(chg), 2) if chg else None,
            "count": b["count"],
        })
    out.sort(key=lambda x: -x["net_inflow_yi"])
    return out


def _fmt_time(value: Any) -> str:
    """涨停池 fbt 是 HHMMSS 整数或字符串。"""
    try:
        text = str(int(value)).zfill(6)
        return f"{text[0:2]}:{text[2:4]}"
    except (TypeError, ValueError):
        return ""


def _sort_key(value: Any) -> float:
    """新闻时间字符串排序用（越新越大），解析失败给 0。"""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(value), "%m-%d %H:%M").timestamp()
    except (TypeError, ValueError):
        return 0


def _get(row: Any, key: str) -> Any:
    """兼容 dict 与 ORM/Row 对象。"""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def build_snapshot(db, market_date: Optional[str] = None, *,
                   with_history: bool = True) -> Dict[str, Any]:
    """抓取 + 聚合，返回完整 payload（不落库）。

    Args:
        db: 用于读当日新闻。
        market_date: 目标交易日 YYYY-MM-DD，空取今天。
        with_history: 是否抓成分股历史（趋势曲线）。首轮结构验证可关掉省时间。
    """
    target = market_date or datetime.now().strftime("%Y-%m-%d")
    compact = target.replace("-", "")
    gaps: List[str] = []

    # ---- 1. 当日行情（一次批量，最关键的块）
    try:
        quotes = fetch_quotes()
    except ThemeCapitalError as exc:
        logger.error("主题资金：%s", exc)
        gaps.append(str(exc))
        quotes = {}

    # ---- 2. 涨停池
    zt_items: List[Dict[str, Any]] = []
    try:
        zt_items = fetch_zt_pool(compact)
        if not zt_items:
            gaps.append("涨停池当日无数据（非交易日或接口为空）")
    except ThemeCapitalError as exc:
        logger.error("主题资金：%s", exc)
        gaps.append(str(exc))

    # ---- 3. 当日新闻（db 为空时跳过，便于脱离服务做结构验证）
    news_rows: List[Any] = []
    if db is not None:
        try:
            news_rows = crud.get_news_list(db, skip=0, limit=300, today_only=True) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("主题资金：读当日新闻失败 %s", exc)
            gaps.append("当日新闻读取失败")

    # ---- 4. 历史净额（趋势）：按成分股求和，篮子固定所以跨日可比
    history_series: Dict[str, List[Dict[str, Any]]] = {}
    if with_history:
        secid_map: Dict[str, List[str]] = {}
        for key, _, _, stocks, _ in CHAINS:
            secid_map[key] = [s["secid"] for s in stocks]
        all_sec = _all_secids()
        hist_by_sec: Dict[str, Dict[str, float]] = {}
        for i, sid in enumerate(all_sec):
            hist_by_sec[sid] = fetch_history(sid)
            if (i + 1) % 10 == 0:
                time.sleep(_REQ_GAP)
        for key, secids in secid_map.items():
            daily: Dict[str, float] = {}
            for sid in secids:
                for date, val in hist_by_sec.get(sid, {}).items():
                    daily[date] = daily.get(date, 0.0) + val
            dates = sorted(daily)[-_HISTORY_DAYS:]
            history_series[key] = [
                {"date": d, "net_inflow_yi": round(daily[d] / 1e8, 2)} for d in dates
            ]
        if not any(history_series.values()):
            gaps.append("成分股资金历史全部抓取失败，趋势曲线暂不可用")
    else:
        for key, _, _, _, _ in CHAINS:
            history_series[key] = []

    # ---- 5. 组装双链
    chains: Dict[str, Any] = {}
    for key, name, subtitle, stocks, keywords in CHAINS:
        chains[key] = _chain_snapshot(key, name, subtitle, stocks, keywords,
                                      quotes, zt_items, news_rows)

    payload: Dict[str, Any] = {
        "market_date": target,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fund_source": "东方财富（主力净额 f62）",
        "chains": chains,
        "history": history_series,
        "data_gaps": gaps,
    }
    payload["item_count"] = sum(c["stock_count"] for c in chains.values())
    return payload


def refresh_theme_capital(db, market_date: Optional[str] = None) -> Dict[str, Any]:
    """抓取 + 聚合 + 落库，返回结果摘要（供 scheduler / API 打日志）。"""
    payload = build_snapshot(db, market_date)
    real_date = payload["market_date"]
    ok = crud.save_theme_capital_snapshot(
        db, real_date, payload, source="eastmoney:theme_capital",
        item_count=int(payload.get("item_count") or 0),
    )
    return {
        "status": "ok" if ok else "error",
        "market_date": real_date,
        "item_count": payload.get("item_count"),
        "chains": {k: {"net_inflow_yi": v["net_inflow_yi"],
                       "leaders": len(v["leaders"])}
                   for k, v in payload["chains"].items()},
        "gaps": payload["data_gaps"],
    }
