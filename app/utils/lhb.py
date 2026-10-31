"""龙虎榜（LHB）抓取、席位分类与打分引擎。

口径全部来自 ``docs/龙虎榜.txt`` 与 ``docs/龙虎榜案例.txt``，实现要点：

1. **数据源**：东方财富数据中心（``datacenter-web.eastmoney.com``），无需 cookie。
   - 榜单：``RPT_DAILYBILLBOARD_DETAILSNEW``（一天一条记录 = 一只股票 × 一个上榜原因）
   - 席位：``RPT_BILLBOARD_DAILYDETAILSBUY`` / ``...SELL``（买卖各前五席位，主键 trade_id 对齐）
   - 位置：``push2his.eastmoney.com`` 日 K 线（算近 20 日涨幅、连板高度）

2. **席位分类**（文档「二、识别席位类型」+「一、席位构成标签」）：
   机构专用 / 北向（沪股通·深股通）/ 游资营业部 / 拉萨天团（散户大本营）/
   外资券商 / 量化，游资再按营业部关键字绑风格标签（席位字典）。

3. **资金结构指标**（文档「三、看资金结构」）：
   龙虎净额、龙虎成交占比、买一集中度、买卖对照（同席位对倒）。

4. **打分**（文档「七、标准化复盘流程」+ 案例的加减分口径）：
   位置 > 席位 > 净额，三日榜权重按 0.4 向 50 分收缩。

⚠ 本模块**只负责抓取与计算**，不碰数据库；落库见 ``app/utils/lhb_refresh.py``。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ================================================================ 席位类型与字典

# 一级类型（文档「一、席位构成标签（一级标签）」）
SEAT_TYPES = {
    "inst": {"label": "机构专用", "tone": "#8b5cf6",
             "note": "公募/保险/资管/QFII 通道；游资可租借通道伪装，勿盲信"},
    "north": {"label": "北向(陆股通)", "tone": "#0ea5e9",
              "note": "沪股通/深股通专用，偏波段与价值，与外资券商营业部不是一回事"},
    "hot_money": {"label": "游资营业部", "tone": "#f59e0b",
                  "note": "绝大多数龙虎席位；风格固定，按席位字典打标"},
    "lhasa": {"label": "拉萨天团", "tone": "#ef4444",
              "note": "东财西藏系列营业部，散户大本营，扎堆上榜 = 抛压预警（反向指标）"},
    "foreign": {"label": "外资券商", "tone": "#14b8a6",
                "note": "瑞银/高盛/摩根大通等在华营业部，配置盘或交易盘，≠北向"},
    "quant": {"label": "量化席位", "tone": "#a855f7",
              "note": "程序化打板，快进快出，无主观长线逻辑"},
    "other": {"label": "其他营业部", "tone": "#94a3b8", "note": "未命中任何规则"},
}

# 席位字典：营业部关键字 -> 江湖名 / 风格标签。
# ⚠ 文档明确要求「只作为初始标签、必须用历史交易数据自动校验」，所以这里只是**初始值**，
#   页面会同时给出该席位近 30 日的实测兑现统计，二者不一致时以统计为准。
# 匹配规则：所有关键字都出现在席位名里才算命中（国泰君安已并入国泰海通，
#   因此关键字尽量取**街道名**，不取券商全名）。
DEPOT_LIBRARY: List[Dict[str, Any]] = [
    {"name": "炒股养家", "keywords": ["华鑫", "红宝石路"],
     "style": ["排板一字", "通道流", "隔日兑现"]},
    {"name": "章盟主", "keywords": ["江苏路"],
     "style": ["大资金波段", "主线龙头", "可锁仓"]},
    {"name": "赵老哥", "keywords": ["银河", "绍兴"],
     "style": ["龙头接力", "点火", "连板为主"]},
    {"name": "孙哥", "keywords": ["中信证券", "溧阳路"],
     "style": ["龙头接力", "情绪核心", "退潮砸得狠"]},
    {"name": "作手新一", "keywords": ["南京太平南路"],
     "style": ["高低切换", "龙头接力", "换手票"]},
    {"name": "陈小群", "keywords": ["银河", "大连黄河路"],
     "style": ["高标连板接力", "高位兑现快"]},
    {"name": "宁波桑田路", "keywords": ["桑田路"],
     "style": ["顶板敢死队", "接力", "一日游"]},
    {"name": "佛山系", "keywords": ["佛山", "绿景路"],
     "style": ["超跌首板", "大长腿", "一日游"]},
    {"name": "成都南北环", "keywords": ["成都", "南一环路"],
     "style": ["潜伏埋伏", "首板套利", "一日游"]},
    {"name": "成都南北环", "keywords": ["成都", "北一环路"],
     "style": ["潜伏埋伏", "首板套利", "一日游"]},
    {"name": "杭州庆春路", "keywords": ["庆春路"],
     "style": ["波段做T", "杭州帮核心"]},
    {"name": "苏南帮", "keywords": ["东吴证券", "苏州"],
     "style": ["联合做T", "短庄滚动", "多席位协同"]},
    {"name": "消闲派", "keywords": ["宜昌沿江大道"],
     "style": ["高位兑现", "砸盘"]},
    {"name": "武汉紫阳东路", "keywords": ["武汉紫阳东路"],
     "style": ["低位补涨", "潜伏做多"]},
]

# 一日游 / 兑现型风格：买榜出现即按「隔日抛压」处理
ONE_DAY_STYLES = ("一日游", "隔日兑现", "高位兑现", "砸盘", "顶板敢死队")
# 接力型风格：买榜出现算加分项
RELAY_STYLES = ("龙头接力", "高标连板接力", "点火", "大资金波段", "主线龙头")

_FOREIGN_KEYWORDS = ("瑞银证券", "高盛", "摩根大通", "摩根士丹利", "汇丰", "野村",
                     "花旗", "瑞信", "德意志", "星展", "大和")
_LHASA_KEYWORDS = ("拉萨", "山南", "昌都", "林芝", "日喀则", "那曲", "阿里")
_QUANT_KEYWORDS = (("华鑫", "瑞平路"), ("华鑫", "陆家嘴"), ("国盛", "宁波分公司"))


def classify_seat(dept_name: str) -> Dict[str, Any]:
    """判断席位一级类型 + 字典标签。

    返回 ``{"type": ..., "alias": ..., "styles": [...], "tags": [...]}``。
    类型优先级刻意如此：机构/北向是交易所口径的硬标识，先认它们；
    拉萨、外资、量化都是「营业部里的特殊群体」，最后才是普通游资。
    """
    name = (dept_name or "").strip()
    out: Dict[str, Any] = {"type": "other", "alias": None, "styles": [], "tags": []}

    if "机构专用" in name:
        out["type"] = "inst"
        out["tags"] = ["机构专用"]
        return out
    if "股通专用" in name:
        out["type"] = "north"
        out["tags"] = ["北向" if "深" not in name else "深股通"]
        return out
    if "东方财富" in name and any(k in name for k in _LHASA_KEYWORDS):
        out["type"] = "lhasa"
        out["alias"] = "拉萨天团"
        out["styles"] = ["散户大本营", "追涨杀跌"]
        out["tags"] = ["拉萨", "反向指标"]
        return out
    if any(k in name for k in _FOREIGN_KEYWORDS):
        out["type"] = "foreign"
        out["tags"] = ["外资券商"]
    elif any(all(k in name for k in pair) for pair in _QUANT_KEYWORDS):
        out["type"] = "quant"
        out["styles"] = ["量化高频"]
        out["tags"] = ["量化"]

    for item in DEPOT_LIBRARY:
        if all(k in name for k in item["keywords"]):
            if out["type"] in ("other", "foreign", "quant"):
                out["type"] = "hot_money" if out["type"] == "other" else out["type"]
            out["alias"] = item["name"]
            out["styles"] = list(item["style"])
            out["tags"] = list(dict.fromkeys(out["tags"] + [item["name"]]))
            break

    if not out["tags"]:
        out["tags"] = ["普通营业部"]
    if out["type"] == "other":
        out["type"] = "hot_money"
    return out


def _is_one_day(styles: Iterable[str]) -> bool:
    return any(any(s in st for s in ONE_DAY_STYLES) for st in (styles or []))


def _is_relay(styles: Iterable[str]) -> bool:
    return any(any(s in st for s in RELAY_STYLES) for st in (styles or []))


# ================================================================ 抓取

_session: Optional[requests.Session] = None

# 各源要求的 Referer（**必须按 host 分开**）：腾讯的行情/日线接口带东财 Referer
# 会直接回 501，东财数据中心则要求自己的域名。之前共用一个 session header，
# 结果腾讯日线在服务进程里全挂（命令行单独跑却正常），排查了很久。
_HOST_HEADERS = {
    "web.ifzq.gtimg.cn": {"Referer": "https://gu.qq.com/"},
    "qt.gtimg.cn": {"Referer": "https://gu.qq.com/"},
    "push2his.eastmoney.com": {"Referer": "https://quote.eastmoney.com/"},
    "datacenter-web.eastmoney.com": {"Referer": "https://data.eastmoney.com/"},
}


def _headers_for(url: str) -> Dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
    }
    for host, extra in _HOST_HEADERS.items():
        if host in url:
            headers.update(extra)
            break
    return headers


def _get(url: str, params: Dict[str, Any], *, timeout: int = 25,
         retries: int = 2) -> Optional[dict]:
    """带重试的 JSON GET。返回 None 表示这次取不到（调用方决定是否放弃）。"""
    global _session
    if _session is None:
        _session = requests.Session()
    last = None
    for attempt in range(retries + 1):
        try:
            resp = _session.get(url, params=params, timeout=timeout,
                                headers=_headers_for(url))
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))
    logger.warning("龙虎榜请求失败 %s %s: %s", url, params.get("reportName") or "", last)
    return None


def _query(report: str, *, columns: str, sort: str, filters: str,
           page_size: int = 500) -> List[dict]:
    """分页取一个报表的全部行（网关/接口单页上限 500 时自动翻页）。"""
    out: List[dict] = []
    for page in range(1, 41):
        data = _get(DATACENTER, {
            "reportName": report, "columns": columns,
            "sortColumns": sort, "sortTypes": "-1",
            "pageNumber": str(page), "pageSize": str(page_size),
            "filter": filters, "source": "WEB", "client": "WEB",
        })
        if not data or not data.get("success"):
            break
        result = data.get("result") or {}
        rows = result.get("data") or []
        out.extend(rows)
        total_pages = result.get("pages") or 1
        if page >= total_pages or not rows:
            break
    return out


DAILY_COLUMNS = (
    "TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,MARKET,CHANGE_TYPE,EXPLANATION,"
    "CLOSE_PRICE,CHANGE_RATE,TURNOVERRATE,FREE_MARKET_CAP,ACCUM_AMOUNT,"
    "BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_NET_AMT,DEAL_AMOUNT_RATIO,TRADE_ID"
)
SEAT_COLUMNS = (
    "TRADE_DATE,TRADE_ID,SECURITY_CODE,OPERATEDEPT_CODE,OPERATEDEPT_NAME,BUY,SELL,NET"
)


def fetch_daily(date: str) -> List[dict]:
    """某交易日全部龙虎榜记录（一条 = 股票 × 上榜原因）。"""
    return _query(
        "RPT_DAILYBILLBOARD_DETAILSNEW", columns=DAILY_COLUMNS,
        sort="BILLBOARD_NET_AMT", filters=f"(TRADE_DATE='{date}')",
    )


def fetch_seats(date: str) -> Tuple[List[dict], List[dict]]:
    """某交易日全部买卖席位明细（买卖各一报表，靠 TRADE_ID 与榜单对齐）。"""
    buy = _query("RPT_BILLBOARD_DAILYDETAILSBUY", columns=SEAT_COLUMNS,
                 sort="TRADE_ID", filters=f"(TRADE_DATE='{date}')", page_size=1000)
    sell = _query("RPT_BILLBOARD_DAILYDETAILSSELL", columns=SEAT_COLUMNS,
                  sort="TRADE_ID", filters=f"(TRADE_DATE='{date}')", page_size=1000)
    return buy, sell


# ---- 日 K 线（判断个股位置：低位 / 中位 / 高位）----
#
# 源选择：**腾讯前复权日线**优先，东财 push2his 兜底。
# 原因（2026-10-29 实测）：sandbox 出口对 push2his.eastmoney.com 会间歇性
# RemoteDisconnected（同一个域名 datacenter-web 却稳定），而腾讯
# web.ifzq.gtimg.cn 一直可用；位置只是打分的一个因子，不值得为它卡住整天数据。
_KLINE_CACHE: Dict[str, Tuple[float, List[dict]]] = {}
_KLINE_TTL = 6 * 3600
_KLINE_MAX_ENTRIES = 4000

# 熔断：连续 N 只股票都取不到日线，就整轮放弃位置计算（默认 30 分钟）。
# 为什么要它：日线是「一只股票一次请求」，如果这个源在当前出口被封，
# 一只股票要白等 2 次超时（约 3~6 秒）× 70 只 = 几分钟，而位置只是打分里的一个因子。
# 熔断后其余股票直接按「无位置」处理，不影响榜单/席位落库。
_KLINE_BREAK = {"streak": 0, "until": 0.0, "limit": 5, "cool": 1800}


def _sina_symbol(code: str, market: str) -> str:
    mk = (market or "").upper()
    if mk == "SH":
        return "sh" + code
    if mk in ("BJ", "BSE"):
        return "bj" + code
    return "sz" + code


def _to_rows(items, close_key: str, date_key: str) -> List[dict]:
    """把 [{date, close}] 收成正序 + 自算涨跌幅（新浪日线不带 pct）。"""
    out: List[dict] = []
    prev = None
    for item in items:
        try:
            close = float(item[close_key])
        except (KeyError, TypeError, ValueError):
            continue
        pct = round((close / prev - 1) * 100, 2) if prev else 0.0
        out.append({"date": str(item[date_key])[:10], "close": close, "pct": pct})
        prev = close
    return out


def _kline_sina(code: str, market: str, days: int) -> List[dict]:
    """新浪日线（首选源）。

    为什么排第一（2026-10-29 实测）：
    - 腾讯 web.ifzq 在沙箱出口被 WAF 拦（返回 501 跳 waf.tencent.com），
    - 东财 push2his 间歇性 RemoteDisconnected，
    - 新浪这个接口稳定、覆盖沪深与北交所（bj 前缀可用），且**只需普通 GET**。
    """
    data = _get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        {"symbol": _sina_symbol(code, market), "scale": "240", "ma": "no",
         "datalen": str(max(30, days))},
        timeout=10, retries=0,
    )
    if not isinstance(data, list):
        return []
    return _to_rows(data, "close", "day")


def _kline_tencent(code: str, market: str, days: int) -> List[dict]:
    """腾讯日线（前复权）兜底。"""
    prefix = _sina_symbol(code, market)
    data = _get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {
        "param": f"{prefix},day,,,{days},qfq",
    }, timeout=8, retries=0)
    node = ((data or {}).get("data") or {}).get(prefix) or {}
    seq = node.get("qfqday") or node.get("day") or []
    items = [{"date": i[0], "close": i[2]} for i in seq if len(i) >= 3]
    return _to_rows(items, "close", "date")


def _kline_eastmoney(code: str, market: str, days: int) -> List[dict]:
    """东财日线（前复权）兜底。腾讯也不可用时才会走到这里。"""
    secid = ("1." if (market or "").upper() == "SH" else "0.") + str(code)
    data = _get(KLINE_API, {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f53,f59,f61",
        "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(days),
    }, timeout=8, retries=0)
    rows: List[dict] = []
    payload = (data or {}).get("data") or {}
    for line in payload.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 4:
            continue
        try:
            rows.append({"date": parts[0], "close": float(parts[1]),
                         "pct": float(parts[2])})
        except (TypeError, ValueError):
            continue
    return rows


def fetch_kline(code: str, market: str, days: int = 90) -> List[dict]:
    """取日 K（正序）。三源依次尝试：新浪 → 腾讯 → 东财。

    取「较长的那一份」而不是「第一个非空的」：某些源对北交所/次新股只回几根
    K 线，必须再问下一个源；但凑到 10 根就够算位置了，不再多问（省请求）。
    """
    key = f"{code}"
    hit = _KLINE_CACHE.get(key)
    if hit and time.time() - hit[0] < _KLINE_TTL:
        return hit[1]
    if time.time() < _KLINE_BREAK["until"]:
        return []

    rows: List[dict] = []
    for fn in (_kline_sina, _kline_tencent, _kline_eastmoney):
        if rows:
            break
        try:
            got = fn(code, market, days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取 %s 日线失败(%s): %s", code, fn.__name__, exc)
            got = []
        if len(got) > len(rows):
            rows = got

    if rows:
        _KLINE_BREAK["streak"] = 0
    else:
        _KLINE_BREAK["streak"] += 1
        if _KLINE_BREAK["streak"] >= _KLINE_BREAK["limit"]:
            _KLINE_BREAK["until"] = time.time() + _KLINE_BREAK["cool"]
            logger.warning("日线接口连续 %d 次取不到数据，本轮位置计算熔断 30 分钟",
                           _KLINE_BREAK["streak"])
            _KLINE_BREAK["streak"] = 0

    if len(_KLINE_CACHE) > _KLINE_MAX_ENTRIES:
        _KLINE_CACHE.clear()
    _KLINE_CACHE[key] = (time.time(), rows)
    return rows


def _limit_pct(code: str, name: str) -> float:
    """涨停幅度阈值（用于数连板）：ST 5%，创业板/科创板 20%，北交所 30%，其余 10%。"""
    nm = (name or "").upper()
    if "ST" in nm:
        return 4.8
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    if code.startswith(("8", "4", "9")):
        return 29.5
    return 9.8


def build_position(code: str, name: str, market: str,
                   trade_date: str) -> Dict[str, Any]:
    """按截至 trade_date 的日 K 判断「低位 / 中位 / 高位」。

    文档强调「位置＞席位＞净额」：同样的龙虎结构，低位首板与高位 5 板含义相反。
    这里只用两个客观量：近 20 日累计涨幅 + 截至当日的连板高度。
    """
    klines = fetch_kline(code, market)
    hist = [k for k in klines if k["date"] <= trade_date]
    if len(hist) < 3:
        return {"position": None, "pos_pct": None, "streak": None,
                "note": "无 K 线数据"}

    # 近 20 日涨幅：上市不足 20 日的次新股用「上市以来」代替，并在 note 里说明，
    # 否则这批票会因为「样本不够」被判成无位置，评分直接丢掉最大权重项。
    window = min(21, len(hist))
    base = hist[-window]["close"]
    pct20 = round((hist[-1]["close"] / base - 1) * 100, 2) if base else None
    note = None if window >= 21 else f"上市不足 20 日，按 {window - 1} 个交易日计"

    limit = _limit_pct(code, name)
    streak = 0
    for k in reversed(hist):
        if k["pct"] >= limit:
            streak += 1
        else:
            break

    if streak >= 4 or (pct20 is not None and pct20 >= 60):
        position = "高位"
    elif streak <= 1 and (pct20 is None or pct20 < 20):
        position = "低位"
    else:
        position = "中位"
    return {"position": position, "pos_pct": pct20, "streak": streak, "note": note}


# ================================================================ 结构与打分

def _fnum(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _scope_of(explanation: str) -> str:
    """上榜类型：三日榜（连续三个交易日）与单日榜必须分开看（文档反复强调）。"""
    text = explanation or ""
    return "3day" if ("连续" in text and "交易日" in text) else "day"


def _net_of(seats: List[dict]) -> Dict[str, float]:
    """按席位类型汇总买卖金额与净额。"""
    agg = {t: {"buy": 0.0, "sell": 0.0, "net": 0.0, "count": 0} for t in SEAT_TYPES}
    for s in seats:
        t = s["seat_type"]
        if t not in agg:
            t = "other"
        agg[t]["buy"] += s["buy"]
        agg[t]["sell"] += s["sell"]
        agg[t]["net"] += s["net"]
        agg[t]["count"] += 1
    return agg


def _combo_tag(buy_agg: Dict[str, dict], sell_agg: Dict[str, dict],
               buy_seats: List[dict]) -> Tuple[str, str]:
    """席位构成组合标签（文档「混合组合标签」）。返回 (标签, 说明)。

    判定顺序即风险优先级：拉萨扎堆 > 机构出逃 > 游资出逃 > 合力 > 单类型。
    「机构出逃」用**买卖两侧的机构金额**判断，而不是只看卖榜有没有机构 ——
    机构同时在两边出现很常见（对倒/换仓），只有卖远大于买才算逃。
    """
    inst_buy = buy_agg["inst"]["buy"]
    inst_sell = sell_agg["inst"]["sell"]
    buy_north = buy_agg["north"]["buy"] > 0
    buy_hot = buy_agg["hot_money"]["buy"] > 0
    lhasa_cnt = buy_agg["lhasa"]["count"]
    sell_hot_net = sell_agg["hot_money"]["net"]

    if lhasa_cnt >= 2:
        return "拉萨扎堆", "多个散户大本营席位包揽买榜，筹码分散、抛压持续"
    if inst_buy == 0 and inst_sell > 0 and (buy_hot or buy_north):
        return "机构出逃·游资接盘", "卖榜机构单边净卖出、买榜无一机构，基本面资金撤退"
    if inst_sell > inst_buy * 1.5 and inst_sell - inst_buy > 0 and buy_hot:
        return "机构出逃·游资接盘", "机构净卖出显著大于买入，短线游资接筹码，退潮无托底"
    if sell_hot_net < 0 and inst_buy > 0:
        return "游资出逃·机构接盘", "游资获利离场、机构接筹码，偏良性换手"
    if inst_buy > 0 and buy_hot:
        return "机构+游资合力", "机构与游资同向买入：题材与基本面共振，连板概率在各组合里相对最高"
    if inst_buy > 0 and buy_north:
        return "机构+北向", "配置型资金同步，波动相对温和"
    if inst_buy > 0:
        return "纯机构", "偏趋势，连板爆发力弱"
    if buy_north and buy_hot:
        return "游资+北向", "外资与短线资金同向，但仍以情绪博弈为主"
    if buy_north:
        return "纯北向", "外资单边买入，参考波段资金"
    if buy_hot:
        return "纯游资", "纯情绪博弈，看席位风格与位置"
    return "其他", "买榜以普通营业部为主"


def _yi(v: Any) -> str:
    """金额→「0.11 亿」这种写法（打分说明里用，和详情卡口径一致）。"""
    try:
        return f"{float(v) / 1e8:.2f} 亿"
    except (TypeError, ValueError):
        return "—"


def score_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """按文档口径打分（0-100）并给出加减分明细、档位、告警。

    权重顺序即文档的「位置＞席位＞净额」：位置单项最大 ±24，席位构成 ±15，
    资金结构（占比/集中度/对倒）合计 ±20，风格再 ±10，三日榜最后统一收缩。

    reasons 每项是结构化的 ``{label, value, delta, note}``（text 保留给老数据）：
    详情页「打分因子拆解」直接拿 label·value 当标题、delta 当右侧得分、
    ``|delta|/20`` 当进度条、note 当解释 —— 文案只在这里写一遍。
    """
    score = 50.0
    reasons: List[Dict[str, Any]] = []

    def add(delta: float, label: str, value: str, note: str) -> None:
        nonlocal score
        score += delta
        reasons.append({
            "delta": round(delta, 1),
            "label": label,
            "value": value,
            "note": note,
            "text": f"{label} {value}：{note}",
        })

    # 1) 位置
    position = record.get("position")
    streak = record.get("streak") or 0
    pos_pct = record.get("pos_pct")
    if position == "低位":
        add(10, "位置", "低位", "低位首板：试盘 / 初建仓，向下空间有限")
    elif position == "中位":
        add(0, "位置", "中位", "中位换手：短线博弈核心区，看接力还是老资金出货")
    elif position == "高位":
        add(-18, "位置", "高位", "高位上榜：默认按兑现视角解读，绝大多数是减仓/回避信号")
    else:
        add(-2, "位置", "缺数据", "K 线未取到，位置未知，按中性处理")
    if streak >= 4:
        add(-6, "连板高度", f"{streak} 连板", "位置极高，任何榜都优先理解为止盈机会")

    # 2) 席位构成
    combo = record.get("combo_tag")
    combo_delta = {
        "机构+游资合力": 12, "机构+北向": 8, "纯机构": 8, "游资+北向": 2,
        "纯北向": 0, "纯游资": 0, "其他": 0,
        "游资出逃·机构接盘": 4, "机构出逃·游资接盘": -12, "拉萨扎堆": -15,
    }.get(combo or "", 0)
    if combo_delta:
        add(combo_delta, "席位结构", combo or "其他",
            record.get("combo_note") or "按买卖榜席位类型组合判定")

    # 3) 龙虎成交占比
    ratio = record.get("deal_ratio")
    if ratio is not None:
        if ratio > 35:
            add(-8, "龙虎成交占比", f"{ratio:.1f}%", "筹码高度集中，容易大起大落")
        elif ratio >= 25:
            add(0, "龙虎成交占比", f"{ratio:.1f}%", "集中度偏高，参考意义打折")
        elif ratio >= 10:
            add(6, "龙虎成交占比", f"{ratio:.1f}%", "占比适中，博弈充分但未过度集中")
        else:
            add(-3, "龙虎成交占比", f"{ratio:.1f}%", "榜外资金主导，参考意义弱")

    # 4) 买一集中度
    b1 = record.get("buy1_ratio")
    if b1 is not None:
        if b1 >= 70:
            add(-8, "买一集中度", f"{b1:.0f}%", "孤庄独买，次日无人接力易砸盘")
        elif b1 >= 60:
            add(-3, "买一集中度", f"{b1:.0f}%", "买一偏独大，接力氛围一般")
        elif b1 < 40:
            add(4, "买一集中度", f"{b1:.0f}%", "买盘分散，多家资金合力")

    # 5) 席位风格（一日游 / 接力）
    one_day_cnt = record.get("one_day_cnt") or 0
    relay_cnt = record.get("relay_cnt") or 0
    if one_day_cnt >= 2:
        add(-10, "一日游席位", f"{one_day_cnt} 个", "次日抛压预警，高开兑现概率大")
    elif one_day_cnt == 1:
        add(-4, "一日游席位", "1 个", "榜上有兑现型席位，隔日看承接")
    if relay_cnt:
        add(5, "知名接力席位", f"{relay_cnt} 个", "接力意愿强，能不能参与看位置")

    # 6) 对倒
    if record.get("wash_trade"):
        add(-8, "对倒做量", "疑似", "同一席位买卖同榜，制造热闹假象吸引跟风")

    # 7) 净额方向
    net = record.get("net_amt")
    if net is not None:
        if net < 0:
            add(-4, "龙虎净额", _yi(net), "买五合计小于卖五合计，抛压偏大")
        elif ratio is not None and ratio <= 30:
            add(2, "龙虎净额", _yi(net), "净买入且集中度可控")

    # 8) 三日榜权重打折（文档：权重 = 单日榜的 0.4）
    if record.get("scope") == "3day":
        score = 50 + (score - 50) * 0.4
        reasons.append({
            "delta": 0, "label": "三日榜折算", "value": "0.4×",
            "note": "合并 3 天买卖额，权重按单日榜 0.4 折算",
            "text": "三日榜：合并 3 天买卖额，权重按单日榜 0.4 折算",
        })

    score = int(round(max(0, min(100, score))))
    grade, level = _grade_of(score)
    alerts = _alerts_of(record, grade)
    return {"score": score, "grade": grade, "level": level,
            "reasons": reasons, "alerts": alerts}


def _grade_of(score: int) -> Tuple[str, str]:
    if score >= 75:
        return "重点观察池", "good"
    if score >= 60:
        return "跟踪观察", "info"
    if score >= 45:
        return "中性", "neutral"
    if score >= 30:
        return "风险池", "warn"
    return "强风险", "danger"


def _alerts_of(record: Dict[str, Any], grade: str) -> List[Dict[str, str]]:
    """告警规则（文档「六、高频陷阱」+ 案例的系统标签）。"""
    out: List[Dict[str, str]] = []
    combo = record.get("combo_tag")
    if combo == "拉萨扎堆":
        out.append({"level": "danger", "tag": "拉萨扎堆",
                    "text": "多个东财拉萨席位包揽买榜：散户接盘，机构在派发，直接回避"})
    if combo == "机构出逃·游资接盘":
        out.append({"level": "danger", "tag": "机构出逃·游资接盘",
                    "text": "卖榜机构大额净卖出、买榜为游资：基本面资金撤退，情绪退潮易崩"})
    if record.get("position") == "高位" and (record.get("one_day_cnt") or 0) >= 1:
        out.append({"level": "danger", "tag": "高位游资诱多",
                    "text": "高位 + 兑现型游资上榜，经典「榜诱多」，优先止盈不追高"})
    if record.get("wash_trade"):
        out.append({"level": "warn", "tag": "对倒做榜",
                    "text": "同一席位买卖同榜，制造热闹假象吸引跟风"})
    if (record.get("buy1_ratio") or 0) >= 70:
        out.append({"level": "warn", "tag": "孤庄独买",
                    "text": "买一独大、其余买二到买五金额很小，接力差"})
    if (record.get("deal_ratio") or 0) > 35:
        out.append({"level": "warn", "tag": "高集中度",
                    "text": "龙虎成交占比 > 35%，筹码集中在少数席位，波动极大"})
    if record.get("scope") == "3day":
        out.append({"level": "info", "tag": "三日榜",
                    "text": "三日合并统计：可能第一天买、第三天卖，净额为正不代表还在买"})
    if (record.get("one_day_cnt") or 0) >= 2:
        out.append({"level": "warn", "tag": "一日游席位主导",
                    "text": "买榜扎堆一日游席位，次日高开兑现概率大"})
    return out


def _styles_of(seat: Dict[str, Any]) -> List[str]:
    """席位风格标签。抓取侧存的是 seat_tags JSON 串，接口侧已经解成 styles 列表。"""
    styles = seat.get("styles")
    if styles is None:
        raw = seat.get("seat_tags")
        if raw:
            try:
                styles = json.loads(raw).get("styles")
            except (ValueError, TypeError):
                styles = None
    return styles or []


def style_counts(buy_seats: List[Dict[str, Any]]) -> Tuple[int, int]:
    """买榜里「一日游」与「知名接力」席位的个数（打分第 5 项）。

    抓取时算一次落进打分，详情接口也要用同口径重算 reasons，所以抽成函数，
    避免两处各写一遍判断而走样。
    """
    one_day_cnt = 0
    relay_cnt = 0
    for s in buy_seats:
        styles = _styles_of(s)
        if _is_one_day(styles):
            one_day_cnt += 1
        if _is_relay(styles):
            relay_cnt += 1
    return one_day_cnt, relay_cnt


def wash_names(buy_seats: List[Dict[str, Any]],
               sell_seats: List[Dict[str, Any]]) -> List[str]:
    """疑似对倒做量的营业部名字列表（空列表＝没有）。

    同一营业部同时出现在买榜与卖榜（机构专用是多席位共用名，需按 dept_code+seq 区分，
    这里只对具名营业部判断，避免把「机构专用」误判成对倒）。
    光「同名出现在两边」噪声太大（大营业部日常既买又卖），再要求两边金额同量级：
    大额买 + 小额卖 = 正常滚动；两边都在千万级且相差不到 3 倍才按对倒提示。
    """
    side: Dict[str, Dict[str, float]] = {}
    for s in list(buy_seats) + list(sell_seats):
        # 「机构专用」是多个机构的共用匿名名；「深股通/沪股通专用」全国只有一张牌，
        # 每天必然两边都有且金额接近，判成对倒纯属噪声 —— 这几类一律不参与对倒判断。
        if "机构专用" in s["dept_name"] or "股通专用" in s["dept_name"]:
            continue
        slot = side.setdefault(s["dept_name"], {"buy": 0.0, "sell": 0.0})
        slot["buy"] += s["buy"]
        slot["sell"] += s["sell"]
    return sorted(
        name for name, v in side.items()
        if v["buy"] >= 5e7 and v["sell"] >= 5e7
        and min(v["buy"], v["sell"]) / max(v["buy"], v["sell"]) >= 0.6
    )


def analyze(record: dict, buy_rows: List[dict], sell_rows: List[dict],
            position: Dict[str, Any]) -> Tuple[Dict[str, Any], List[dict]]:
    """把一条榜单 + 其买卖席位整理成落库行 + 席位行。"""
    # ⚠ 东财这两张明细报表**不保证按金额排序**（实测 002865 的 BUY 表第一行
    # 2865 万、第二行 6305 万），而「买1」这个名次本身是有含义的（买一集中度、
    # 孤庄独买告警都直接取第 1 行），所以必须自己按金额降序重排名次。
    buy_rows = sorted(buy_rows, key=lambda r: _fnum(r.get("BUY")) or 0.0, reverse=True)
    sell_rows = sorted(sell_rows, key=lambda r: _fnum(r.get("SELL")) or 0.0, reverse=True)

    seats: List[dict] = []
    for flag, rows in (("buy", buy_rows), ("sell", sell_rows)):
        for idx, row in enumerate(rows, start=1):
            dept = (row.get("OPERATEDEPT_NAME") or "").strip()
            info = classify_seat(dept)
            seats.append({
                "flag": flag,
                "seq": idx,
                "dept_code": str(row.get("OPERATEDEPT_CODE") or ""),
                "dept_name": dept,
                "buy": _fnum(row.get("BUY")) or 0.0,
                "sell": _fnum(row.get("SELL")) or 0.0,
                "net": _fnum(row.get("NET")) or 0.0,
                "seat_type": info["type"],
                "seat_tags": json.dumps(
                    {"alias": info["alias"], "styles": info["styles"],
                     "tags": info["tags"]}, ensure_ascii=False),
            })

    buy_seats = [s for s in seats if s["flag"] == "buy"]
    sell_seats = [s for s in seats if s["flag"] == "sell"]
    buy_agg = _net_of(buy_seats)
    sell_agg = _net_of(sell_seats)

    buy_total = sum(s["buy"] for s in buy_seats)
    buy1_ratio = round(buy_seats[0]["buy"] / buy_total * 100, 1) if (buy_seats and buy_total) else None

    # 对倒：同一营业部同时出现在买榜与卖榜，且两边金额同量级（判定见 wash_names）
    wash = wash_names(buy_seats, sell_seats)

    one_day_cnt, relay_cnt = style_counts(buy_seats)

    combo_tag, combo_note = _combo_tag(buy_agg, sell_agg, buy_seats)

    row = {
        "trade_date": record["date"],
        "trade_id": record["trade_id"],
        "code": record["code"],
        "name": record["name"],
        "market": record["market"],
        "change_type": record["change_type"],
        "explanation": record["explanation"],
        "scope": _scope_of(record["explanation"]),
        "close": record["close"],
        "change_rate": record["change_rate"],
        "turnover_rate": record["turnover_rate"],
        "free_mcap": record["free_mcap"],
        "accum_amount": record["accum_amount"],
        "buy_amt": record["buy_amt"],
        "sell_amt": record["sell_amt"],
        "net_amt": record["net_amt"],
        "deal_ratio": record["deal_ratio"],
        "buy1_ratio": buy1_ratio,
        # 净额口径：**买榜该类型的买入额 − 卖榜该类型的卖出额**，不要拿两表的 net 相加。
        # 原因：同一个席位（尤其「深股通/沪股通专用」只有一张牌）常常同时出现在买榜与
        # 卖榜两张明细表里、且两表的 BUY/SELL 是同一组数，net 相加会把这一笔算两遍
        # （实测 002865 北向买 6305 万、卖 6511 万，正确净额 −206 万，加两表就变 −412 万）。
        "inst_net": round(buy_agg["inst"]["buy"] - sell_agg["inst"]["sell"], 2),
        "north_net": round(buy_agg["north"]["buy"] - sell_agg["north"]["sell"], 2),
        "hot_net": round(buy_agg["hot_money"]["buy"] - sell_agg["hot_money"]["sell"], 2),
        "seat_types": json.dumps({
            "buy": {t: {"count": v["count"], "buy": round(v["buy"], 2),
                        "net": round(v["net"], 2)}
                    for t, v in buy_agg.items() if v["count"]},
            "sell": {t: {"count": v["count"], "sell": round(v["sell"], 2),
                         "net": round(v["net"], 2)}
                     for t, v in sell_agg.items() if v["count"]},
        }, ensure_ascii=False),
        "combo_tags": json.dumps([combo_tag, combo_note], ensure_ascii=False),
        "position": position.get("position"),
        "pos_pct": position.get("pos_pct"),
        "streak": position.get("streak"),
        "one_day_cnt": one_day_cnt,
        "relay_cnt": relay_cnt,
        "wash_trade": bool(wash),
    }

    result = score_record({**row, "combo_tag": combo_tag, "combo_note": combo_note})
    row.update({
        "score": result["score"],
        "grade": result["grade"],
        "alerts": json.dumps(result["alerts"], ensure_ascii=False),
        "reasons": json.dumps(result["reasons"], ensure_ascii=False),
    })
    # 这三个只是打分用的中间量，云库表里没有对应列（有列反而要跟着改 DDL）
    for k in ("one_day_cnt", "relay_cnt", "wash_trade"):
        row.pop(k, None)
    for s in seats:
        s.update({"trade_date": record["date"], "trade_id": record["trade_id"],
                  "code": record["code"], "name": record["name"]})
    return row, seats


def build_day(date: str, *, with_position: bool = True) -> Dict[str, Any]:
    """抓取并分析某日全部龙虎榜数据。

    返回 ``{"records": [...], "seats": [...], "errors": [...], "stats": {...}}``。
    席位明细按 TRADE_ID 归并；缺失席位不影响榜单落库（只在记录上标注）。
    """
    raw = fetch_daily(date)
    buy_rows, sell_rows = fetch_seats(date)
    errors: List[str] = []

    seats_by_trade: Dict[str, Dict[str, List[dict]]] = {}
    for flag, rows in (("buy", buy_rows), ("sell", sell_rows)):
        for row in rows:
            tid = str(row.get("TRADE_ID") or "")
            if not tid:
                continue
            seats_by_trade.setdefault(tid, {"buy": [], "sell": []})[flag].append(row)

    records: List[dict] = []
    seats_out: List[dict] = []
    for item in raw:
        tid = str(item.get("TRADE_ID") or "")
        record = {
            "date": (item.get("TRADE_DATE") or date)[:10],
            "trade_id": tid,
            "code": item.get("SECURITY_CODE") or "",
            "name": item.get("SECURITY_NAME_ABBR") or "",
            "market": item.get("MARKET") or "",
            "change_type": str(item.get("CHANGE_TYPE") or ""),
            "explanation": item.get("EXPLANATION") or "",
            "close": _fnum(item.get("CLOSE_PRICE")),
            "change_rate": _fnum(item.get("CHANGE_RATE")),
            "turnover_rate": _fnum(item.get("TURNOVERRATE")),
            "free_mcap": _fnum(item.get("FREE_MARKET_CAP")),
            "accum_amount": _fnum(item.get("ACCUM_AMOUNT")),
            "buy_amt": _fnum(item.get("BILLBOARD_BUY_AMT")),
            "sell_amt": _fnum(item.get("BILLBOARD_SELL_AMT")),
            "net_amt": _fnum(item.get("BILLBOARD_NET_AMT")),
            "deal_ratio": _fnum(item.get("DEAL_AMOUNT_RATIO")),
        }
        if not tid:
            continue
        pair = seats_by_trade.get(tid) or {"buy": [], "sell": []}
        if not pair["buy"] and not pair["sell"]:
            errors.append(f"{record['name']}({record['code']}) 缺席位明细")

        position: Dict[str, Any] = {"position": None, "pos_pct": None, "streak": None}
        if with_position:
            try:
                position = build_position(record["code"], record["name"],
                                          record["market"], record["date"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("取 %s K 线失败: %s", record["code"], exc)

        row, seat_rows = analyze(record, pair["buy"], pair["sell"], position)
        records.append(row)
        seats_out.extend(seat_rows)

    records.sort(key=lambda r: (r["score"], r["net_amt"] or 0), reverse=True)
    return {
        "records": records,
        "seats": seats_out,
        "errors": errors,
        "stats": _day_stats(raw, records),
    }


def one_row_per_stock(records: List[dict]) -> List[dict]:
    """同一只股票当天因多个上榜原因会有多条记录，但买五卖五是同一组席位。

    汇总「龙虎净额合计 / 机构净买入 / 北向净买入」这类**金额**指标时必须按个股去重，
    否则一只股票上榜 2 个原因就把它的净额算两遍。优先取单日榜（金额＝当日真实成交），
    同日多条时取累计成交额更大的那条。
    """
    best: Dict[str, dict] = {}

    def rank(x: dict) -> Tuple[int, float]:
        return (1 if x.get("scope") != "3day" else 0, x.get("accum_amount") or 0.0)

    for r in records:
        key = r.get("code") or r.get("trade_id") or ""
        cur = best.get(key)
        if cur is None or rank(r) > rank(cur):
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------- 场景分类
#
# 口径：``docs/龙虎榜案例.txt`` 末尾那张「案例对照清单」的七类（系统标签 → 场景 + 处理策略），
# 顺序、文案、处理策略都按文档原样搬过来，只额外补了一个兜底桶 other。
# 判定是**单桶互斥**的：一只股票当天只能落在一个场景里，优先级按「危害大的先落位」——
# 拉萨扎堆/机构出逃最严重，其次高位诱多、一日游，再往下才是趋势与机会类。
SCENES: List[Dict[str, Any]] = [
    {"key": "watch", "label": "优质观察信号", "icon": "✅", "tone": "good",
     "tag": "机构+游资合力",
     "structure": "低位主线、机构与接力游资同向买入，买盘分散、买一不独大",
     "strategy": "加入观察池，等盘面确认，不直接买入"},
    {"key": "relay", "label": "中性换手", "icon": "⚖️", "tone": "info",
     "tag": "游资轮换接力",
     "structure": "中位连板，老游资卖出、新游资接力，机构不集中砸盘",
     "strategy": "情绪向上才小仓位参与，退潮期放弃"},
    {"key": "trap", "label": "高危诱多", "icon": "⚠️", "tone": "warn",
     "tag": "高位游资诱多",
     "structure": "高位 + 兑现型游资大单买入，或三日榜净额好看",
     "strategy": "回避，持仓分批止盈"},
    {"key": "oneday", "label": "一日游套利", "icon": "⚠️", "tone": "warn",
     "tag": "一日游席位主导",
     "structure": "佛山系/成都南北环等一日游席位包揽买榜，独立题材无板块效应",
     "strategy": "直接放弃接力"},
    {"key": "knife", "label": "飞刀结构", "icon": "❌", "tone": "danger",
     "tag": "机构出逃·游资接盘",
     "structure": "卖榜机构大额净卖出，买榜是短线游资接筹码",
     "strategy": "减仓 / 回避"},
    {"key": "retail", "label": "散户接盘", "icon": "❌", "tone": "danger",
     "tag": "拉萨扎堆",
     "structure": "多个拉萨席位包揽买榜，筹码分散、抛压持续",
     "strategy": "拉黑回避"},
    {"key": "trend", "label": "波段趋势", "icon": "📈", "tone": "good",
     "tag": "趋势机构票",
     "structure": "连续多个交易日机构（含北向）净买入，龙虎占比不高",
     "strategy": "波段跟踪，不短线打板"},
    {"key": "other", "label": "其他结构", "icon": "·", "tone": "muted",
     "tag": "不在清单内",
     "structure": "不满足上面七类的形态（如高位纯机构、缺位置数据等）",
     "strategy": "仅作参考，不构成明确信号"},
]

SCENE_BY_KEY = {s["key"]: s for s in SCENES}


def scene_of(record: Dict[str, Any], inst_streak: int = 0) -> str:
    """把一条记录归入「案例对照清单」里的某个场景，返回场景 key。

    ``inst_streak`` 是该股（含当日）连续上榜且机构净买入的天数，用来识别
    「连续多日机构 + 北向加仓」的趋势机构票 —— 单日记录本身看不出这件事。
    """
    combo = (record.get("combo_tag") or "").strip()
    tags = {a.get("tag") for a in (record.get("alerts") or []) if isinstance(a, dict)}
    pos = record.get("position")
    scope = record.get("scope")

    # ❌ 最严重：散户接盘 / 飞刀结构，先落位，避免被后面的「合力」类覆盖
    if combo == "拉萨扎堆" or "拉萨扎堆" in tags:
        return "retail"
    if combo == "机构出逃·游资接盘" or "机构出逃·游资接盘" in tags:
        return "knife"

    # ⚠️ 高位默认按「兑现视角」读：吃到兑现型游资、三日榜、纯游资结构的都算诱多
    if pos == "高位" and (
        "高位游资诱多" in tags
        or scope == "3day"
        or combo in ("纯游资", "游资+北向", "游资出逃·机构接盘")
    ):
        return "trap"

    # ⚠️ 一日游席位主导（买榜扎堆一日游）
    if "一日游席位主导" in tags:
        return "oneday"

    # 📈 连续 3 日以上机构净买入且不是高位 → 趋势机构票（文档：适合波段，重点看连续多日）
    if inst_streak >= 3 and pos != "高位":
        return "trend"

    # ⚠️ 剩下的高位（高位 + 机构/北向但没游资）文档也是「默认预警、兑现视角」
    if pos == "高位":
        return "trap"

    # ✅ 低位 + 机构与游资（或北向）同向买入 → 优质观察信号
    if pos == "低位" and combo in ("机构+游资合力", "机构+北向"):
        return "watch"

    # ⚖️ 换手接力结构：中位连板一律归这一档（买卖双方都有资金，不是一边倒）；
    # 低位里「老游资卖出、机构/新资金接盘」以及纯游资纯北向，属于同一族形态
    if pos == "中位":
        return "relay"
    if pos == "低位" and combo in (
        "游资出逃·机构接盘", "游资+北向", "纯游资", "纯北向", "其他"
    ):
        return "relay"

    # 位置未知（缺日线）或低位纯机构单日：位置＞席位，说不清就不硬套
    return "other"


def scene_counts(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """按场景统计条数（前端筛选按钮上的数字）。"""
    out = {s["key"]: 0 for s in SCENES}
    for r in records:
        key = r.get("scene") or "other"
        if key in out:
            out[key] += 1
    return out


def _day_stats(raw: List[dict], records: List[dict]) -> Dict[str, Any]:
    """当日概览（按记录聚合，同时给出个股去重口径）。"""
    codes = {r["code"] for r in records}
    uniq = one_row_per_stock(records)
    net = sum((r.get("net_amt") or 0) for r in uniq)
    inst = sum((r.get("inst_net") or 0) for r in uniq)
    north = sum((r.get("north_net") or 0) for r in uniq)
    alerts = 0
    for r in records:
        alerts += len(json.loads(r.get("alerts") or "[]"))
    return {
        "rows": len(records),
        "stocks": len(codes),
        "net_amt": round(net, 2),
        "inst_net": round(inst, 2),
        "north_net": round(north, 2),
        "alerts": alerts,
        "high_position": sum(1 for r in records if r.get("position") == "高位"),
        "lhasa": sum(1 for r in records if "拉萨扎堆" in (r.get("combo_tags") or "")),
        "three_day": sum(1 for r in records if r.get("scope") == "3day"),
    }


# ================================================================ 席位实测统计

def seat_stats_from_rows(rows: List[dict]) -> Dict[str, Any]:
    """由已落库的席位行算一个席位的「实测画像」。

    rows 需按 trade_date 升序、且含 trade_date / code / flag / buy / sell。
    输出文档要求的四个量化指标里，能在库内直接算的两个：
      上榜天数、隔日兑现率（次日在该股卖出 > 0 的占比）。
    ⚠ 隔日兑现率的分母只算「有次日数据」的观测 —— 只有 1 天数据时返回 None，
      否则会显示成「0% 从不兑现」，把样本不足伪装成一个很好的结论。
    """
    from collections import defaultdict

    by_code: Dict[str, Dict[str, List[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_code[r["code"]][r["trade_date"]].append(r)

    days = len({r["trade_date"] for r in rows if r.get("flag") == "buy"})
    buy_appear = 0
    pairs = 0
    sold_next = 0
    for code, by_date in by_code.items():
        dates = sorted(by_date)
        for i, d in enumerate(dates):
            buys = [r for r in by_date[d] if r.get("flag") == "buy"]
            if not buys:
                continue
            buy_appear += 1
            if i + 1 < len(dates):
                pairs += 1
                nxt = by_date[dates[i + 1]]
                if any((r.get("sell") or 0) > 0 for r in nxt):
                    sold_next += 1
    return {
        "days": days,
        "buy_appear": buy_appear,
        "pairs": pairs,
        "next_day_sell_ratio": round(sold_next / pairs * 100, 1) if pairs else None,
    }
