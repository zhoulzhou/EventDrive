"""中国人民银行官网（www.pbc.gov.cn）数据抓取。

补齐 akshare 未覆盖的指标，改由央行官方发布渠道直接抓取：

======================  ==========================================  ==========
指标                    官方发布渠道                                 频率
======================  ==========================================  ==========
7天逆回购操作利率        《公开市场业务交易公告》                       每个交易日
社融存量同比             《金融统计数据报告》（数据解读栏目）             每月
分部门存贷款             《金融统计数据报告》第四、五节（累计口径）        每月
（住户/企业/非银/财政、企业与住户中长贷）
======================  ==========================================  ==========

设计约定：
- 每个 fetch_* 函数独立失败，异常由调用方按「该指标抓取失败」处理，不影响其他指标。
- 返回 {"value": float, "as_of": str, "detail": dict}。
- 页面编码为 UTF-8；沙箱环境可能注入失效代理，故显式关闭 trust_env。
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 站点根地址候选（https 不可用时回退 http）
_BASE_CANDIDATES = ("https://www.pbc.gov.cn", "http://www.pbc.gov.cn")
_working_base: Optional[str] = None

# 各栏目索引页
COL_OMO = "/zhengcehuobisi/125207/125213/125431/125475/index.html"        # 公开市场业务交易公告
COL_FIN_STAT = "/diaochatongjisi/116219/116225/index.html"                # 金融统计数据报告

_HTML_TIMEOUT = 25


# ---------------------------------------------------------------- HTTP 基础

def _session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    # 沙箱/CI 常注入失效代理，直接连接更可靠
    sess.trust_env = False
    return sess


def _get(url: str, timeout: int = _HTML_TIMEOUT, base: Optional[str] = None) -> requests.Response:
    """GET 一个央行页面，自动处理编码与站点根地址回退。"""
    global _working_base
    if base is None:
        base = _working_base
    candidates = [base] if base else list(_BASE_CANDIDATES)
    last_exc: Optional[Exception] = None
    for root in candidates:
        full = url if url.startswith("http") else root + url
        try:
            resp = _session().get(full, timeout=timeout)
            resp.raise_for_status()
            if resp.headers.get("Content-Type", "").startswith("text/"):
                resp.encoding = "utf-8"
            if base is None:
                _working_base = root
            return resp
        except Exception as exc:  # 换下一个根地址重试
            last_exc = exc
            logger.debug("央行页面请求失败 %s: %s", full, exc)
    raise RuntimeError("无法访问央行页面 %s: %s" % (url, last_exc))


def _strip_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&ldquo;", "“"),
                         ("&rdquo;", "”"), ("&mdash;", "—")):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def _page_text(url: str) -> str:
    return _strip_html(_get(url).text)


def _links(html: str) -> List[Tuple[str, str]]:
    """抽取页面里 (标题, 链接) 列表，标题做去空白处理。"""
    found = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{2,160})</a>', html)
    return [(re.sub(r"\s+", "", title), href) for href, title in found]


def _recent_links(html: str, keyword: str, limit: int = 12) -> List[Tuple[str, str]]:
    """栏目索引页按时间倒序，取标题含 keyword 的最近若干条。"""
    hits: List[Tuple[str, str]] = []
    for title, href in _links(html):
        if keyword in title:
            hits.append((title, href))
            if len(hits) >= limit:
                break
    return hits


def _cn_ym_to_iso(text: str) -> Optional[str]:
    """从「2026年8月...」中取出 2026-08。"""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if not m:
        return None
    return "%s-%02d" % (m.group(1), int(m.group(2)))


# ---------------------------------------------------------------- 逆回购操作利率


# 逆回购期限：央行按流动性需要选择操作期限（近年以隔夜为主）
_REPO_TERMS = ("隔夜", "7天", "14天", "28天")


def _parse_repo_row(text: str) -> Optional[Tuple[str, float]]:
    """从《公开市场业务交易公告》正文里解析逆回购操作期限与利率。

    页面表格渲染成 "…期限 操作 利率 投标量 中标量 隔夜 1. 25 % 3795 亿元…"，
    先去空白再匹配「期限 + 利率」。公告可能同时做多个期限，取表格中第一个。
    """
    flat = re.sub(r"\s+", "", text).replace("．", ".").replace("％", "%")
    if "逆回购" not in flat:
        return None
    for term in _REPO_TERMS:
        # 标题「隔夜逆回购操作情况」后面不带利率，正则会自动跳到表格中的操作行
        m = re.search(r"%s(?:期)?[^0-9%%]{0,16}?(\d+\.\d+)%%" % term, flat)
        if m:
            return term, float(m.group(1))
    return None


def _announce_date(text: str) -> Optional[str]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return "%s-%s-%s" % m.groups() if m else None


def fetch_reverse_repo_rate() -> Dict[str, Any]:
    """抓取最近一期公开市场逆回购操作利率（政策利率锚，期限随公告变化）。

    期限可能是隔夜、7 天、14 天等，按公告实际披露返回；向前回溯若干条公告，
    取最新一条能解析出「期限 + 利率」的。
    """
    html = _get(COL_OMO).text
    candidates = _recent_links(html, "公开市场业务交易公告", limit=12)
    if not candidates:
        raise RuntimeError("《公开市场业务交易公告》栏目未解析到任何条目")

    last_error: Optional[Exception] = None
    for title, href in candidates:
        try:
            text = _page_text(href)
        except Exception as exc:
            last_error = exc
            continue
        parsed = _parse_repo_row(text)
        if parsed is None:
            continue
        term, rate = parsed
        return {
            "value": rate,
            "as_of": _announce_date(text) or "",
            "term": term,
            "detail": {
                "公告": title,
                "期限": term,
                "来源": "中国人民银行公开市场业务交易公告",
                "链接": href if href.startswith("http") else (_working_base or _BASE_CANDIDATES[0]) + href,
            },
        }
    raise RuntimeError("最近公告中未找到逆回购操作利率（%s）" % (last_error or "均无可解析的操作行"))


# ---------------------------------------------------------------- 社融存量同比

_TSF_PATTERN = re.compile(
    r"社会融资规模存量为\s*([\d.]+)\s*万亿元[，,]?\s*同比增长\s*([\d.]+)\s*%"
)


def fetch_tsf_stock_yoy() -> Dict[str, Any]:
    """从月度《金融统计数据报告》抓取社会融资规模存量同比。"""
    html = _get(COL_FIN_STAT).text
    candidates = _recent_links(html, "金融统计数据报告", limit=8)
    if not candidates:
        raise RuntimeError("《金融统计数据报告》栏目未解析到任何条目")

    last_error: Optional[Exception] = None
    for title, href in candidates:
        try:
            text = _page_text(href)
        except Exception as exc:
            last_error = exc
            continue
        m = _TSF_PATTERN.search(text)
        if not m:
            continue
        stock_wan_yi = float(m.group(1))
        return {
            "value": float(m.group(2)),
            "as_of": _report_period(title) or _cn_ym_to_iso(text) or "",
            "detail": {
                "报告": title,
                "存量": "%s 万亿元" % m.group(1),
                "存量万亿": stock_wan_yi,
                "来源": "中国人民银行《金融统计数据报告》",
            },
        }
    raise RuntimeError("未能解析社融存量同比（%s）" % (last_error or "报告正文无匹配字段"))


# ---------------------------------------------------------------- 分部门存贷款
#
# ③「部门结构」维度看的是「钱在谁手里」，取《金融统计数据报告》第四、五节的
# 分部门数据。注意口径是**年初至今累计**（"前八个月人民币存款增加…其中住户存款
# 增加 6.99 万亿元"），不是当月增量 —— 央行月度报告只披露累计数。
#
# 正文里同时出现存款方与贷款方两个列表（如"非银行业金融机构存款增加…"与
# "非银行业金融机构贷款减少…"），所以匹配式必须带上「存款」/「贷款」以免串档。

_AMOUNT_PATTERN = r"([\d.]+)\s*(万亿元|亿元)"
_DEPOSIT_SPLIT_FIELDS = {
    "住户存款": "住户存款",
    "住户贷款": "住户贷款",
    "非银行业金融机构存款": "非银存款",
    "非金融企业存款": "企业存款",
}


def _amount_to_wan_yi(num: str, unit: str) -> float:
    """「亿元」→「万亿元」；「万亿元」原样返回。"""
    value = float(num)
    return round(value / 10000.0, 4) if unit == "亿元" else round(value, 4)


def _parse_ml_loan(flat: str, anchor: str, span: int = 180) -> Optional[float]:
    """解析「住户贷款…其中…中长期贷款减少 501 亿元」的累计值（万亿元）。

    同段正文里住户与企业各有一句中长期贷款，因此先定位分部门锚点，
    再在锚点后的有限窗口内取第一条「中长期贷款」读数，避免串档。
    """
    idx = flat.find(anchor)
    if idx < 0:
        return None
    segment = flat[idx: idx + span]
    m = re.search(r"中长期贷款\s*(增加|减少|下降|上升)\s*([\d.]+)\s*(万亿元|亿元)", segment)
    if not m:
        return None
    value = _amount_to_wan_yi(m.group(2), m.group(3))
    return -value if m.group(1) in ("减少", "下降") else value


def _parse_fiscal_deposit(flat: str) -> Optional[float]:
    m = re.search(r"财政性存款\s*(增加|减少|下降|上升)\s*([\d.]+)\s*(万亿元|亿元)", flat)
    if not m:
        return None
    value = _amount_to_wan_yi(m.group(2), m.group(3))
    return -value if m.group(1) in ("减少", "下降") else value



_REPORT_PERIODS = (
    (re.compile(r"(\d{4})\s*年\s*一季度"), 3),
    (re.compile(r"(\d{4})\s*年\s*上半年"), 6),
    (re.compile(r"(\d{4})\s*年\s*前三季度"), 9),
    (re.compile(r"(\d{4})\s*年\s*年度"), 12),
)


def _report_period(title: str) -> Optional[str]:
    """由报告标题推出数据期：2026年8月→2026-08；上半年→2026-06；一季度→2026-03。

    季度/半年类的标题里没有「N 月」，_cn_ym_to_iso 会返回 None，故先按这些关键词归一。
    """
    for pattern, month in _REPORT_PERIODS:
        m = pattern.search(title)
        if m:
            return "%s-%02d" % (m.group(1), month)
    return _cn_ym_to_iso(title)


# 报告正文里的窗口描述：「前八个月 / 上半年 / 前三季度 / 一季度 / 8月份」
_REPORT_WINDOW = r"(前[一二三四五六七八九十]+个月|上半年|前三季度|前?一季度|\d{1,2}月份)"
_WINDOW_RE = re.compile(_REPORT_WINDOW)

# 「前八个月人民币存款增加 18.99 万亿元」——温度计里「企业/非银存款占新增存款比」的分母
_TOTAL_DEPOSIT_RE = re.compile(_REPORT_WINDOW + r"人民币存款增加" + _AMOUNT_PATTERN)
_TOTAL_DEPOSIT_LOOSE_RE = re.compile(r"人民币存款增加" + _AMOUNT_PATTERN)


def _wan_yi(value: float, unit: str) -> float:
    """把「亿元」折算成「万亿元」；本来就是「万亿元」则原样返回。"""
    return round(value / 10000.0, 4) if unit == "亿元" else round(value, 4)


def _parse_split_amount(flat: str, name: str) -> Optional[float]:
    """解析「住户存款增加 6.99 万亿元」/「住户贷款减少 8271 亿元」→ 万亿元（减少为负）。

    取第一条匹配（报告中同一分部门只出现一次）。
    """
    pattern = re.compile(
        r"%s\s*(增加|减少|下降|上升)\s*%s" % (re.escape(name), _AMOUNT_PATTERN)
    )
    m = pattern.search(flat)
    if not m:
        return None
    value = float(m.group(2))
    if m.group(3) == "亿元":
        value = value / 10000.0
    sign = -1.0 if m.group(1) in ("减少", "下降") else 1.0
    return round(sign * value, 4)


def _parse_split_report(title: str, flat: str) -> Optional[Dict[str, Any]]:
    """解析**单期**《金融统计数据报告》的分部门存贷款与新增存款总额（均为累计口径）。

    解析不出分部门数据时返回 None，由调用方继续往前翻一期。
    """
    if "住户存款" not in flat:
        return None

    parsed = {
        label: _parse_split_amount(flat, needle)
        for needle, label in _DEPOSIT_SPLIT_FIELDS.items()
    }
    if parsed.get("住户贷款") is None and parsed.get("住户存款") is None:
        return None

    window = None
    total = None
    m = _TOTAL_DEPOSIT_RE.search(flat)
    if m:
        window = m.group(1)
        total = _wan_yi(float(m.group(2)), m.group(3))
    if total is None:  # 标题/正文里没有窗口词（个别月份报告）时的兜底
        m2 = _TOTAL_DEPOSIT_LOOSE_RE.search(flat)
        if m2:
            total = _wan_yi(float(m2.group(1)), m2.group(2))
    if window is None:
        w = _WINDOW_RE.search(flat)
        window = w.group(1) if w else "累计"

    return {
        "报告": title,
        "as_of": _report_period(title) or _cn_ym_to_iso(flat) or "",
        "期间": window,
        "人民币存款增加": total,
        "住户存款": parsed.get("住户存款"),
        "住户贷款": parsed.get("住户贷款"),
        "非银存款": parsed.get("非银存款"),
        "企业存款": parsed.get("企业存款"),
        "住户中长期贷款": _parse_ml_loan(flat, "住户贷款"),
        "企业中长期贷款": _parse_ml_loan(flat, "业单位贷款"),
        "财政性存款": _parse_fiscal_deposit(flat),
    }


def _month_before(period: str) -> Optional[str]:
    """YYYY-MM → 上一个月；1 月返回 None（当月增量即年初至今累计）。"""
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, IndexError):
        return None
    if month == 1:
        return None
    return "%04d-%02d" % (year, month - 1)


def _same_month_last_year(period: str) -> Optional[str]:
    try:
        return "%04d-%s" % (int(period[:4]) - 1, period[5:7])
    except (ValueError, IndexError):
        return None


def fetch_credit_breakdown(periods: int = 16) -> Dict[str, Any]:
    """抓取近若干期报告，计算企业与住户中长期贷款「同比多增」及财政存款当月增量。

    央行报告只披露**年初至今累计**增量，因此：
    - 当月增量 = 当期累计 − 上期累计（1 月直接取累计）
    - 同比多增 = 当月增量 − 去年同月增量

    periods 默认 16 期，覆盖「本月 + 上月 + 去年同月 + 去年上月」。
    返回 {as_of, corp_ml_yoy, household_ml_yoy, fiscal_deposit_mom, detail}（单位：亿元）。
    """
    html = _get(COL_FIN_STAT).text
    candidates = _recent_links(html, "金融统计数据报告", limit=max(periods + 6, 12))
    if not candidates:
        raise RuntimeError("《金融统计数据报告》栏目未解析到任何条目")

    records: List[Dict[str, Any]] = []
    last_error: Optional[Exception] = None
    for title, href in candidates:
        if len(records) >= periods:
            break
        try:
            flat = re.sub(r"\s+", "", _page_text(href))
        except Exception as exc:
            last_error = exc
            continue
        record = _parse_split_report(title, flat)
        if record and record.get("as_of"):
            records.append(record)

    if not records:
        raise RuntimeError("未能解析中长期贷款/财政存款（%s）" % (last_error or "报告正文无匹配字段"))

    by_period = {r["as_of"]: r for r in records if r.get("as_of")}
    latest_period = max(by_period)
    latest = by_period[latest_period]

    def _flow(period: Optional[str], field: str) -> Optional[float]:
        """某字段的当月增量（亿元）。"""
        if not period:
            return None
        rec = by_period.get(period)
        if not rec or rec.get(field) is None:
            return None
        cum = float(rec[field])
        prev_period = _month_before(period)
        if prev_period is None:
            return round(cum * 10000.0, 0)
        prev = by_period.get(prev_period)
        if not prev or prev.get(field) is None:
            return None
        return round((cum - float(prev[field])) * 10000.0, 0)

    def _yoy_diff(field: str) -> Optional[float]:
        cur = _flow(latest_period, field)
        last = _flow(_same_month_last_year(latest_period), field)
        if cur is None or last is None:
            return None
        return round(cur - last, 0)

    corp_now = _flow(latest_period, "企业中长期贷款")
    corp_last = _flow(_same_month_last_year(latest_period), "企业中长期贷款")
    house_now = _flow(latest_period, "住户中长期贷款")
    house_last = _flow(_same_month_last_year(latest_period), "住户中长期贷款")
    fiscal_now = _flow(latest_period, "财政性存款")

    def _fmt(value: Optional[float]) -> str:
        return "—" if value is None else "%d 亿" % value

    return {
        "as_of": latest_period,
        "corp_ml_yoy": _yoy_diff("企业中长期贷款"),
        "household_ml_yoy": _yoy_diff("住户中长期贷款"),
        "fiscal_deposit_mom": fiscal_now,
        "detail": {
            "报告": latest.get("报告"),
            "数据期": latest_period,
            "企业中长期贷款": "当月 %s · 去年同期 %s" % (_fmt(corp_now), _fmt(corp_last)),
            "住户中长期贷款": "当月 %s · 去年同期 %s" % (_fmt(house_now), _fmt(house_last)),
            "财政性存款": "当月 %s" % _fmt(fiscal_now),
            "口径": "当月增量 = 相邻两期累计相减；同比多增 = 当月增量 − 去年同月增量",
            "来源": "中国人民银行《金融统计数据报告》分部门存贷款",
        },
    }


def fetch_credit_series(periods: int = 30) -> List[Dict[str, Any]]:
    """回溯各报告期的分部门信贷读数（单位：亿元），供趋势图使用。

    与 fetch_credit_breakdown 同一套解析与口径，但逐期返回：
    {as_of, corp_ml_yoy, household_ml_yoy, fiscal_deposit_mom}。
    同比多增需要「去年同月」的累计值，periods 太小时最早几期会缺同比，故默认取 30 期。
    """
    html = _get(COL_FIN_STAT).text
    candidates = _recent_links(html, "金融统计数据报告", limit=max(periods + 8, 18))
    if not candidates:
        raise RuntimeError("《金融统计数据报告》栏目未解析到任何条目")

    by_period: Dict[str, Dict[str, Any]] = {}
    for title, href in candidates:
        try:
            flat = re.sub(r"\s+", "", _page_text(href))
        except Exception:
            continue
        record = _parse_split_report(title, flat)
        if record and record.get("as_of"):
            by_period[record["as_of"]] = record

    def _flow(period: Optional[str], field: str) -> Optional[float]:
        """某期该字段的当月增量（亿元）。"""
        if not period:
            return None
        rec = by_period.get(period)
        if not rec or rec.get(field) is None:
            return None
        cum = float(rec[field])
        prev_period = _month_before(period)
        if prev_period is None:
            return round(cum * 10000.0, 0)
        prev = by_period.get(prev_period)
        if not prev or prev.get(field) is None:
            return None
        return round((cum - float(prev[field])) * 10000.0, 0)

    rows: List[Dict[str, Any]] = []
    for period in sorted(by_period):
        corp = _flow(period, "企业中长期贷款")
        corp_ly = _flow(_same_month_last_year(period), "企业中长期贷款")
        house = _flow(period, "住户中长期贷款")
        house_ly = _flow(_same_month_last_year(period), "住户中长期贷款")
        rows.append({
            "as_of": period,
            "corp_ml_yoy": None if corp is None or corp_ly is None else round(corp - corp_ly, 0),
            "household_ml_yoy": None if house is None or house_ly is None else round(house - house_ly, 0),
            "fiscal_deposit_mom": _flow(period, "财政性存款"),
        })
    return rows



