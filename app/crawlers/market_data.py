#!/usr/bin/env python3
"""市场数据抓取模块（多源多活：每个指标一条降级链，均无需 API Key）。

数据源组合（**前面的不通或不够新鲜才用后面的**，实测均免 Key）：
- 纳斯达克100（NDX）
    1. ``nasdaq_api``    api.nasdaq.com 官方报价（权威，自带交易日期）
    2. ``tencent_quote`` qt.gtimg.cn 腾讯美股指数快照（与官方收盘一致）
    3. ``tencent_kline`` web.ifzq.gtimg.cn 腾讯美股指数日线（一次多日，可补缺口）
    4. ``em_quote``      push2.eastmoney.com 东财全球指数 100.NDX100
    5. ``fred``          FRED NASDAQ100
- VIX 恐慌指数
    1. ``cboe_csv``      CBOE 官方历史 CSV（含最近 30 个交易日）
    2. ``cboe_json``     CBOE 延时报价 JSON（CSV 改名/404 时兜底）
    3. ``fred``          FRED VIXCLS
- 美债 2Y / 10Y 收益率
    1. ``treasury``      美国财政部每日收益率 CSV（含最近 30 个交易日）
    2. ``em_bond``       东财「中美国债收益率」（一次拿 2Y/10Y 两列，与财政部一致）
    3. ``fred``          FRED DGS2 / DGS10

为什么要降级链（2026-10-31 排查结论）：
  原先 NDX 只有 api.nasdaq.com + FRED 两条腿，两条腿同时不通时 NDX 会**连续多日停更**
  （实测 10-27~10-30 四天没落库，页面一直停在 10-26），而同期 VIX / 美债是正常的，
  所以页面表现为「只有一个指数不动」。现在每类数据都有 3~5 个独立来源。

数据获取走「定时落库 + 页面读库」模式：
- 调度器调用 refresh_market_data() 抓取并写入 market_prices 表；
- 页面接口从数据库读取，直接访问外部源的只有 /api/market/sources 探针（诊断用）。

补漏（catch-up）：
  源返回的不止一个点（CSV / 日线），落库时只写「比库里最新日期更新」的那些点，
  所以任何一天断掉，下一次抓取会自动把缺口补齐，并按日期顺序推进峰值回撤状态。
"""
import asyncio
import calendar
import csv
import io
import logging
import time
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from app.database import SessionLocal
from app import crud, schemas
from app.market_strategy import advance, rebuild_state
from app.utils.anti_crawl import get_limiter, RateLimitExceeded

logger = logging.getLogger(__name__)

# 美东时区：判断美股是否开盘、以及「最近一个已收盘交易日」
US_EASTERN = ZoneInfo("America/New_York")

# follow_redirects=True：部分数据源（如 CBOE）已把 cdn.cboe.com 迁移到 cdn-api.cboe.com，
# 旧地址会返回 307 重定向；不跟随重定向会直接抛 HTTPStatusError 导致该指标抓不到。
_client = httpx.AsyncClient(timeout=20, follow_redirects=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
)
JSON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# 腾讯/东财的行情接口都要带 Referer，否则返回空或 403
TENCENT_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"}
EM_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://quote.eastmoney.com/"}
EM_DATA_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"}

# 各数据源端点
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
# CBOE 已把数据域名迁移到 cdn-api.cboe.com；旧域名 cdn.cboe.com 会 307 重定向到此。
VIX_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_JSON_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
# 纳斯达克官方实时报价（主源，自带交易日期）
NASDAQ_URL = "https://api.nasdaq.com/api/quote/NDX/info?assetclass=index"
# 腾讯：美股指数快照 + 美股指数日线（补缺口）
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q=usNDX"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
# 东财：全球指数 100.NDX100（注意 100.NDX 是「纳斯达克综合」，不是 100 指数）
EM_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 东财：中美国债收益率（美国 2Y = EMG00001306，10Y = EMG00001310）
EM_BOND_URL = "https://datacenter.eastmoney.com/api/data/get"
EM_BOND_TOKEN = "894050c76af8597a853f5b408b759f5d"

# 单次抓取每个来源最多取多少个交易日（够补一周多的缺口，也不会把历史表灌爆）
HISTORY_POINTS = 30

SOURCE_NAMES = {
    "nasdaq_api": "Nasdaq 官方",
    "tencent_quote": "腾讯证券",
    "tencent_kline": "腾讯证券(日线)",
    "em_quote": "东方财富",
    "em_bond": "东方财富(中美国债)",
    "cboe_csv": "CBOE",
    "cboe_json": "CBOE(延时报价)",
    "fred": "FRED",
    "treasury": "美国财政部",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """返回某月第 n 个 weekday（weekday: 0=周一 ... 6=周日）。"""
    days = [
        d for d in (
            date(year, month, day)
            for day in range(1, calendar.monthrange(year, month)[1] + 1)
        ) if d.weekday() == weekday
    ]
    return days[n - 1]


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """返回某月最后一个 weekday。"""
    days = [
        d for d in (
            date(year, month, day)
            for day in range(1, calendar.monthrange(year, month)[1] + 1)
        ) if d.weekday() == weekday
    ]
    return days[-1]


def _easter(year: int) -> date:
    """高斯复活节算法（Annexe de la date de Pâques），用于推算耶稣受难日(Good Friday)。"""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_holiday(d: date) -> date:
    """美股"补假"规则：固定日期假日若落在周六则提前至周五休市，落在周日则顺延至周一。"""
    if d.weekday() == 5:  # 周六
        return d - timedelta(days=1)
    if d.weekday() == 6:  # 周日
        return d + timedelta(days=1)
    return d


def _us_holidays(year: int):
    """生成给定年份的美股主要休市日集合（规则计算，跨年自动生效）。"""
    s = {
        _observed_holiday(date(year, 1, 1)),                            # New Year's Day
        _nth_weekday(year, 1, 0, 3),                                    # MLK Day
        _nth_weekday(year, 2, 0, 3),                                    # Presidents' Day
        _easter(year) - timedelta(days=2),                              # Good Friday
        _last_weekday(year, 5, 0),                                      # Memorial Day
        _observed_holiday(date(year, 6, 19)),                           # Juneteenth
        _observed_holiday(date(year, 7, 4)),                            # Independence Day
        _nth_weekday(year, 9, 0, 1),                                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                                   # Thanksgiving
        _observed_holiday(date(year, 12, 25)),                          # Christmas
    }
    return s


def _is_us_market_open(today: date) -> bool:
    """判断给定美东日期是否为美股交易日：排除周末与主要节假日。"""
    if today.weekday() >= 5:  # 周六/周日
        return False
    return today not in _us_holidays(today.year)


def latest_closed_session(now_et: Optional[datetime] = None) -> str:
    """最近一个**已收盘**的美股交易日（YYYY-MM-DD）。

    取数时刻（北京时间 9:15 / 21:34 = 美东 21:15 / 09:34）决定了这个日期：
    - 美东 16:05 之后（或非交易日）→ 当天/前一个交易日已收盘；
    - 美东盘中或开盘前 → 只能取到上一个交易日。

    它有两个用处：① 判断库里数据是否已经够新（省掉无意义的请求）；
    ② 判断主源返回的行情是不是「陈旧」的，陈旧就继续往下走备用源。
    """
    now_et = now_et or datetime.now(US_EASTERN)
    d = now_et.date()
    if _is_us_market_open(d) and now_et.time() >= dtime(16, 5):
        return d.isoformat()
    d -= timedelta(days=1)
    for _ in range(10):  # 最多回退 10 天，长假也够
        if _is_us_market_open(d):
            return d.isoformat()
        d -= timedelta(days=1)
    return d.isoformat()


async def _get_limited(url: str, **kwargs) -> httpx.Response:
    """带限流的异步 GET：申请令牌 -> 发请求 -> 成功/失败回报限流器。

    所有外部数据源（FRED / CBOE / Treasury / Nasdaq / 腾讯 / 东财）都走这里，
    统一受限流器约束：同一 host 强制最小请求间隔、单次任务请求数封顶、
    失败指数退避、每日配额封顶。
    """
    limiter = get_limiter()
    await limiter.acquire_async(url)
    try:
        response = await _client.get(url, **kwargs)
        response.raise_for_status()
    except RateLimitExceeded:
        raise
    except Exception as e:
        cool = limiter.report_failure(url)
        logger.error(f"请求 {url} 失败: {e}（退避 {cool:.0f}s）", exc_info=True)
        raise
    limiter.report_success(url)
    return response


# ----------------------------------------------------------------------
# 解析小工具
# ----------------------------------------------------------------------
def _parse_mdy(value: str) -> str:
    """MM/DD/YYYY -> YYYY-MM-DD（与数据库 date 格式保持一致）。"""
    m, d, y = value.split("/")
    return f"{y}-{m}-{d}"


_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _parse_nasdaq_ts(ts: str) -> str:
    """解析 Nasdaq 报价时间，如 'Aug 21, 2026' -> '2026-08-21'。"""
    try:
        parts = ts.replace(",", "").split()
        if len(parts) != 3:
            return ""
        month, day, year = parts
        return f"{year}-{_MONTHS[month[:3]]:02d}-{int(day):02d}"
    except Exception:
        return ""


def _point(d: str, v: Any) -> Optional[Dict[str, Any]]:
    """构造一个 (日期, 数值) 点；日期/数值不合法则返回 None。"""
    if not d:
        return None
    try:
        value = float(v)
    except (TypeError, ValueError):
        return None
    return {"date": d[:10], "value": value}


def _et_date_from_ts(ts: Any) -> str:
    """Unix 秒级时间戳 -> 美东交易日（东财给的收盘时间 16:00 ET，按北京时区换算会串天）。"""
    try:
        return datetime.fromtimestamp(int(ts), US_EASTERN).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _trim(points: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """去重 + 按日期升序 + 只保留最近 limit 个交易日。"""
    by_date: Dict[str, float] = {}
    for p in points:
        if p:
            by_date[p["date"]] = p["value"]
    return [{"date": d, "value": by_date[d]} for d in sorted(by_date)][-limit:]


# ----------------------------------------------------------------------
# 抓取器：每个函数返回「按日期升序的点列表」（只含日期/数值）
# ----------------------------------------------------------------------
async def _f_nasdaq_api(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """纳斯达克官方报价：从 primaryData 取 lastSalePrice 与 lastTradeTimestamp。"""
    response = await _get_limited(NASDAQ_URL, headers=JSON_HEADERS)
    primary = (response.json().get("data") or {}).get("primaryData") or {}
    last_sale = (primary.get("lastSalePrice") or "").replace(",", "").strip()
    ts = (primary.get("lastTradeTimestamp") or "").strip()
    p = _point(_parse_nasdaq_ts(ts), last_sale)
    if not p:
        raise ValueError(f"Nasdaq 官方返回不可解析: price={last_sale!r} ts={ts!r}")
    return [p]


async def _f_tencent_quote(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """腾讯美股指数快照。字段以 ~ 分隔：3=最新价，30=时间（美东日期）。"""
    response = await _get_limited(TENCENT_QUOTE_URL, headers=TENCENT_HEADERS)
    text = response.content.decode("gbk", errors="ignore")
    body = text.split('"')[1] if '"' in text else ""
    fields = body.split("~")
    if len(fields) < 31:
        raise ValueError(f"腾讯快照字段不足({len(fields)})：{text[:80]!r}")
    p = _point(fields[30][:10], fields[3])
    if not p:
        raise ValueError(f"腾讯快照不可解析: price={fields[3]!r} ts={fields[30]!r}")
    return [p]


async def _f_tencent_kline(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """腾讯美股指数日线：[[日期, 开, 收, 高, 低, 量], ...]，取收盘价（下标 2）。"""
    response = await _get_limited(
        TENCENT_KLINE_URL,
        params={"param": f"usNDX,day,,,{min(limit, 60)},qfq"},
        headers=TENCENT_HEADERS,
    )
    node = ((response.json().get("data") or {}).get("usNDX") or {})
    rows = node.get("qfqday") or node.get("day") or []
    points = [_point(r[0], r[2]) for r in rows if isinstance(r, list) and len(r) >= 3]
    points = [p for p in points if p]
    if not points:
        raise ValueError("腾讯日线返回为空")
    return _trim(points, limit)


async def _f_em_quote(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """东财全球指数 100.NDX100：f43=最新价(×100)，f86=行情时间戳。"""
    response = await _get_limited(
        EM_QUOTE_URL,
        params={"secid": "100.NDX100", "fields": "f43,f57,f58,f86"},
        headers=EM_HEADERS,
    )
    data = response.json().get("data") or {}
    raw = data.get("f43")
    if raw in (None, "-", 0):
        raise ValueError(f"东财 NDX100 无数据: {data}")
    p = _point(_et_date_from_ts(data.get("f86")), float(raw) / 100.0)
    if not p:
        raise ValueError(f"东财 NDX100 不可解析: {data}")
    return [p]


async def _f_fred(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """FRED 序列 CSV（date,value），取最近 limit 行。"""
    response = await _get_limited(FRED_URL, params={"id": series["fred_id"]})
    points = []
    for row in list(csv.reader(io.StringIO(response.text)))[1:]:
        if len(row) < 2:
            continue
        raw = row[1].strip()
        if not raw or raw in (".", ""):
            continue
        p = _point(row[0].strip(), raw)
        if p:
            points.append(p)
    if not points:
        raise ValueError(f"FRED {series['fred_id']} 返回为空")
    return _trim(points, limit)


async def _f_vix_csv(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """CBOE VIX 官方历史 CSV：DATE,OPEN,HIGH,LOW,CLOSE（CLOSE 下标 4）。"""
    response = await _get_limited(VIX_URL)
    points = []
    for row in list(csv.reader(io.StringIO(response.text)))[1:]:
        if len(row) < 5 or not row[0].strip():
            continue
        close = row[4].strip()
        if not close:
            continue
        p = _point(_parse_mdy(row[0].strip()), close)
        if p:
            points.append(p)
    if not points:
        raise ValueError("CBOE VIX CSV 返回为空")
    return _trim(points, limit)


async def _f_vix_json(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """CBOE 延时报价 JSON：data.current_price + data.last_trade_time。"""
    response = await _get_limited(VIX_JSON_URL, headers=JSON_HEADERS)
    data = response.json().get("data") or {}
    p = _point((data.get("last_trade_time") or "")[:10], data.get("current_price"))
    if not p:
        raise ValueError(f"CBOE 延时报价不可解析: {data}")
    return [p]


# 财政部全量 CSV 一次要 10~20 秒（整年、上千行），2Y 和 10Y 用的是**同一个文件**，
# 同一轮里只该下载一次：按年份缓存 5 分钟，并用锁合并并发的两次请求。
_TREASURY_CACHE: Dict[int, Tuple[float, List[List[str]]]] = {}
_TREASURY_LOCK = asyncio.Lock()
_TREASURY_TTL = 300.0


async def _treasury_rows(year: int) -> List[List[str]]:
    """取某年美国财政部收益率 CSV 的原始行（同轮内复用，避免重复下载）。"""
    hit = _TREASURY_CACHE.get(year)
    if hit and time.monotonic() - hit[0] < _TREASURY_TTL:
        return hit[1]
    async with _TREASURY_LOCK:
        hit = _TREASURY_CACHE.get(year)
        if hit and time.monotonic() - hit[0] < _TREASURY_TTL:
            return hit[1]
        response = await _get_limited(TREASURY_URL.format(year=year))
        rows = list(csv.reader(io.StringIO(response.text)))
        _TREASURY_CACHE[year] = (time.monotonic(), rows)
        return rows


async def _f_treasury(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """美国财政部当年每日收益率 CSV：首行即最新，按 列名 取值。"""
    column = series["column"]
    # 跨年：1 月初当年 CSV 还没几条数据，同时把去年也拉一次，靠日期排序去重
    years = [datetime.now(US_EASTERN).year]
    if datetime.now(US_EASTERN).month == 1:
        years.append(years[0] - 1)
    points: List[Dict[str, Any]] = []
    errors: List[str] = []
    for year in years:
        try:
            rows = await _treasury_rows(year)
        except Exception as e:  # noqa: BLE001 - 单年失败不影响另一年
            errors.append(f"{year}: {type(e).__name__}: {e}")
            continue
        if not rows:
            errors.append(f"{year}: 空响应")
            continue
        header = [c.strip() for c in rows[0]]
        if column not in header:
            errors.append(f"{year}: 无 {column} 列 {header}")
            continue
        idx = header.index(column)
        for row in rows[1:]:
            if len(row) <= idx:
                continue
            p = _point(_parse_mdy(row[0].strip()), row[idx].strip())
            if p:
                points.append(p)
    if not points:
        raise ValueError("财政部 CSV 无有效数据: " + "; ".join(errors))
    return _trim(points, limit)


async def _f_em_bond(series: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """东财中美国债收益率：美国 2Y = EMG00001306，10Y = EMG00001310。"""
    column = series["column"]  # 与财政部 CSV 列名同构：'2 Yr' / '10 Yr'
    field = "EMG00001306" if column.startswith("2") else "EMG00001310"
    response = await _get_limited(
        EM_BOND_URL,
        params={
            "type": "RPTA_WEB_TREASURYYIELD", "sty": "ALL", "st": "SOLAR_DATE",
            "sr": "-1", "token": EM_BOND_TOKEN, "p": "1", "ps": str(max(limit, 10)),
            "pageNo": "1", "pageNum": "1",
        },
        headers=EM_DATA_HEADERS,
    )
    rows = ((response.json().get("result") or {}).get("data")) or []
    points = []
    for r in rows:
        p = _point((r.get("SOLAR_DATE") or "")[:10], r.get(field))
        if p:
            points.append(p)
    if not points:
        raise ValueError("东财中美国债收益率返回为空")
    return _trim(points, limit)


FETCHERS: Dict[str, Callable[..., Any]] = {
    "nasdaq_api": _f_nasdaq_api,
    "tencent_quote": _f_tencent_quote,
    "tencent_kline": _f_tencent_kline,
    "em_quote": _f_em_quote,
    "fred": _f_fred,
    "cboe_csv": _f_vix_csv,
    "cboe_json": _f_vix_json,
    "treasury": _f_treasury,
    "em_bond": _f_em_bond,
}

# 每个指标的数据源降级链（顺序 = 优先级）
SERIES = [
    {
        "key": "nasdaq", "name": "纳斯达克100指数", "symbol": "NASDAQ100", "unit": "",
        "describe": "NASDAQ-100 Index", "fred_id": "NASDAQ100", "column": None,
        "sources": ["nasdaq_api", "tencent_quote", "tencent_kline", "em_quote", "fred"],
    },
    {
        "key": "vix", "name": "VIX恐慌指数", "symbol": "VIXCLS", "unit": "",
        "describe": "CBOE Volatility Index", "fred_id": "VIXCLS", "column": None,
        "sources": ["cboe_csv", "cboe_json", "fred"],
    },
    {
        "key": "dgs2", "name": "美债2年期收益率", "symbol": "DGS2", "unit": "%",
        "describe": "U.S. Treasury 2-Year Yield", "fred_id": "DGS2", "column": "2 Yr",
        "sources": ["treasury", "em_bond", "fred"],
    },
    {
        "key": "dgs10", "name": "美债10年期收益率", "symbol": "DGS10", "unit": "%",
        "describe": "U.S. Treasury 10-Year Yield", "fred_id": "DGS10", "column": "10 Yr",
        "sources": ["treasury", "em_bond", "fred"],
    },
]

# 单次抓取任务要预留的开始标记（重置每个 host 的单次请求计数）
_ALL_SOURCE_URLS = {
    "nasdaq_api": NASDAQ_URL,
    "tencent_quote": TENCENT_QUOTE_URL,
    "tencent_kline": TENCENT_KLINE_URL,
    "em_quote": EM_QUOTE_URL,
    "em_bond": EM_BOND_URL,
    "cboe_csv": VIX_URL,
    "cboe_json": VIX_JSON_URL,
    "treasury": TREASURY_URL,
    "fred": FRED_URL,
}


async def _collect_points(
    series: Dict[str, Any], expected: str, limit: int = HISTORY_POINTS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按降级链取数，返回 (按日期升序的点, 每个来源的尝试记录)。

    停止条件：已覆盖到 ``expected``（最近一个已收盘交易日）**且**已拿到多日数据
    —— 多日数据用来补历史缺口，所以单点来源（官方报价 / 快照）成功后仍会再走
    一个带历史的来源（腾讯日线），成本 2 个请求。
    """
    merged: Dict[str, float] = {}
    trail: List[Dict[str, Any]] = []

    for sid in series["sources"]:
        fn = FETCHERS[sid]
        t0 = time.monotonic()
        try:
            points = await fn(series, limit)
            error = None
        except RateLimitExceeded as e:
            points, error = [], f"限流: {e}"
        except Exception as e:  # noqa: BLE001 - 任何异常都只降级，不中断整轮
            points, error = [], f"{type(e).__name__}: {e}"

        for p in points:
            if p:
                merged.setdefault(p["date"], p["value"])

        trail.append({
            "source": sid,
            "source_name": SOURCE_NAMES.get(sid, sid),
            "points": len(points),
            "latest": points[-1]["date"] if points else None,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": error,
        })
        if error:
            logger.warning(f"{series['name']}: 来源 {sid} 失败（{error}），尝试下一个来源")
        else:
            logger.info(
                f"{series['name']}: 来源 {sid} 取到 {len(points)} 个点"
                f"（最新 {points[-1]['date'] if points else '-'}）"
            )

        if expected and merged and max(merged) >= expected and len(merged) >= 2:
            break
        if expected and merged and max(merged) >= expected and sid == series["sources"][-1]:
            break

    points = [{"date": d, "value": merged[d]} for d in sorted(merged)]
    return points, trail


async def fetch_market_points(expected: Optional[str] = None) -> List[Dict[str, Any]]:
    """并发抓取全部指标（原始数据，不落库）。"""
    expected = expected or latest_closed_session()
    results = await asyncio.gather(
        *(_collect_points(s, expected) for s in SERIES), return_exceptions=True
    )
    items: List[Dict[str, Any]] = []
    for series, res in zip(SERIES, results):
        if isinstance(res, Exception):
            logger.error(f"{series['name']}: 抓取整体失败: {res}", exc_info=res)
            points, trail = [], [{"source": "all", "error": str(res)}]
        else:
            points, trail = res
        latest = points[-1] if points else None
        used = [t["source_name"] for t in trail if not t.get("error") and t.get("points")]
        items.append({
            "key": series["key"],
            "name": series["name"],
            "symbol": series["symbol"],
            "unit": series["unit"],
            "describe": series["describe"],
            "value": latest["value"] if latest else None,
            "date": latest["date"] if latest else None,
            "points": points,
            "sources": used,
            "trail": trail,
        })
    return items


# 兼容旧调用名（此前只抓 FRED，保留别名避免外部引用断裂）
async def fetch_fred_prices() -> List[Dict[str, Any]]:
    return await fetch_market_points()


def save_market_prices(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把抓到的点按 (symbol, date) 落库，只写「比库里最新日期更新」的部分。

    同步函数，供 asyncio.to_thread 调用。

    补漏逻辑：每个指标本次可能带回多个交易日（CSV / 日线），只写库里还没有的
    那些日期，并按日期升序逐条推进峰值回撤状态 —— 这样断掉的那几天会在下一次
    抓取时自动补齐，峰值/回撤也不会因为跳日而算错。
    """
    db = SessionLocal()
    summary: Dict[str, Any] = {"saved": 0, "per_symbol": {}}
    try:
        cutoff = (datetime.now(US_EASTERN).date() - timedelta(days=60)).isoformat()
        existing = crud.get_market_dates_since(db, cutoff)
        for item in items:
            points = item.get("points") or []
            if not points:
                logger.info(f"  - {item['name']}: 所有来源均无数据，跳过落库")
                summary["per_symbol"][item["symbol"]] = {"added": 0, "latest": None}
                continue

            have = existing.get(item["symbol"]) or set()
            # 缺的点分两种：比库里最新日期新的（尾部缺口），以及中间断掉的那几天
            missing = [p for p in points if p["date"] not in have]
            last = max(have) if have else None
            trailing = [p for p in missing if not last or p["date"] > last]
            if not missing:
                logger.info(f"  - {item['name']}: 数据无更新（库内最新 {last}），跳过")
                summary["per_symbol"][item["symbol"]] = {"added": 0, "latest": last}
                continue

            # 一次批量 upsert（云库下单条 0.25~0.9s，补 30 天逐条会撞网关超时）
            crud.upsert_market_prices(db, [
                schemas.MarketPriceCreate(
                    symbol=item["symbol"],
                    name=item["name"],
                    unit=item["unit"],
                    value=p["value"],
                    date=p["date"],
                )
                for p in missing
            ])
            saved_dates = [p["date"] for p in missing]
            # 只对「比原来最新更晚」的点推进峰值：它们构成连续的新历史，
            # 按日期升序推进才不会把回撤重置判反。中间空缺由 rebuild_state 统一重算。
            for p in sorted(trailing, key=lambda x: x["date"]):
                advance(db, item["symbol"], p["value"], p["date"])

            if any(p["date"] <= (last or "") for p in missing) and last:
                # 补了中间空缺 → 峰值可能被低估，按完整历史重算一次
                rebuilt = rebuild_state(db, item["symbol"])
                if rebuilt.get("rebuilt"):
                    logger.info(
                        f"  - {item['name']}: 峰值按完整历史重算 → "
                        f"{rebuilt['peak_value']} ({rebuilt['peak_date']})"
                    )

            summary["saved"] += len(saved_dates)
            summary["per_symbol"][item["symbol"]] = {
                "added": len(saved_dates),
                "latest": max(saved_dates),
                "from": min(saved_dates),
                "sources": item.get("sources") or [],
            }
            logger.info(
                f"  - {item['name']}: 新增 {len(saved_dates)} 天"
                f"（{min(saved_dates)} ~ {max(saved_dates)}），来源 "
                f"{'/'.join(item.get('sources') or ['-'])}"
            )
    finally:
        db.close()
    return summary


def _source_label(items: List[Dict[str, Any]]) -> str:
    """把各指标实际用到的来源拼成一句话（写进 crawl_logs.source，便于线上排查）。"""
    parts = []
    for item in items:
        used = item.get("sources") or []
        parts.append(f"{item['symbol']}={used[0] if len(used) == 1 else '/'.join(used)}")
    return " · ".join(parts)


async def _db_dates_since(since: str) -> Dict[str, set]:
    """读库内 since 之后各指标已落库的日期集合（云库/本地都走 crud，避免裸 SQLAlchemy 读空）。"""
    def _run() -> Dict[str, set]:
        db = SessionLocal()
        try:
            return crud.get_market_dates_since(db, since)
        finally:
            db.close()
    return await asyncio.to_thread(_run)


def recent_sessions(expected: str, n: int = 5) -> List[str]:
    """从 expected（最近已收盘交易日）往前取 n 个交易日，用于判断「最近这段是否齐」。"""
    out: List[str] = []
    d = date.fromisoformat(expected)
    while len(out) < n:
        if _is_us_market_open(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


async def refresh_market_data(force: bool = False) -> Dict[str, Any]:
    """抓取全市场指标并落库（按 symbol+date 累积历史），返回原始数据供日志使用。

    Args:
        force: True 时不做「数据已最新」的短路，强制把所有来源走一遍（补漏用）。
    """
    expected = latest_closed_session()
    # 「最近 5 个交易日是否齐」比「最新日期够不够新」更严格：
    # 中间断一天（某天抓取失败、之后又恢复）不会被误判为「已最新」。
    # ⚠ recent_sessions() 是**从新到旧**排列，sessions[0] 是最新那天；
    #   取 cutoff 必须用 min()，否则 cutoff = 最新日期，判断恒为「不齐」，
    #   跳过逻辑等于没写（2026-10-31 修）。
    sessions = recent_sessions(expected, 5)
    oldest = min(sessions)

    if not force:
        try:
            have = await _db_dates_since(oldest)
        except Exception as e:  # noqa: BLE001 - 读库失败不影响抓取
            logger.warning(f"读取库内已有日期失败，按需抓取处理: {e}")
            have = {}
        missing = [
            s["symbol"] for s in SERIES
            if not set(sessions) <= (have.get(s["symbol"]) or set())
        ]
        if not missing:
            logger.info(f"市场行情最近 5 个交易日已齐（{oldest}~{expected}），跳过抓取")
            return {
                "items": [],
                "skipped": True,
                "reason": f"最近 5 个交易日（{oldest}~{expected}）已齐",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": f"跳过(已最新 {expected})",
            }
        logger.info(f"市场行情待补: {', '.join(missing)}（目标 {expected}）")

    # 标记一次完整抓取任务：重置各 host 的单次计数
    limiter = get_limiter()
    for url in _ALL_SOURCE_URLS.values():
        limiter.begin_run(url)

    items = await fetch_market_points(expected)
    summary = await asyncio.to_thread(save_market_prices, items)
    return {
        "items": items,
        "summary": summary,
        "expected": expected,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": _source_label(items),
    }


async def probe_sources() -> Dict[str, Any]:
    """逐来源体检：每个指标的每个来源都真打一次，返回值/日期/耗时/错误。

    只供 /api/market/sources 诊断用，不落库、不影响定时任务。
    """
    expected = latest_closed_session()
    series_out = []
    for series in SERIES:
        rows = []
        for sid in series["sources"]:
            t0 = time.monotonic()
            try:
                points = await FETCHERS[sid](series, HISTORY_POINTS)
                error = None
            except RateLimitExceeded as e:
                points, error = [], f"限流: {e}"
            except Exception as e:  # noqa: BLE001
                points, error = [], f"{type(e).__name__}: {e}"
            latest = points[-1] if points else None
            rows.append({
                "source": sid,
                "source_name": SOURCE_NAMES.get(sid, sid),
                "ok": error is None,
                "points": len(points),
                "latest_date": latest["date"] if latest else None,
                "latest_value": latest["value"] if latest else None,
                "fresh": bool(latest and latest["date"] >= expected),
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "error": error,
            })
        series_out.append({
            "symbol": series["symbol"],
            "name": series["name"],
            "expected": expected,
            "sources": rows,
        })
    return {"expected": expected, "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "series": series_out}
