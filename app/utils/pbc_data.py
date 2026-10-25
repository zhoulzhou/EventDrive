"""中国人民银行官网（www.pbc.gov.cn）数据抓取。

补齐 akshare 未覆盖的指标，改由央行官方发布渠道直接抓取：

======================  ==========================================  ==========
指标                    官方发布渠道                                 频率
======================  ==========================================  ==========
7天逆回购操作利率        《公开市场业务交易公告》                       每个交易日
社融存量同比             《金融统计数据报告》（数据解读栏目）             每月
分部门存贷款             《金融统计数据报告》第四、五节（累计口径）        每月
（住户/企业/非银/财政，
含中长期贷款与财政存款）
======================  ==========================================  ==========

设计约定：
- 每个 fetch_* 函数独立失败，异常由调用方按「该指标抓取失败」处理，不影响其他指标。
- 返回 {"value": float, "as_of": str, "detail": dict}。
  （原「超储率」分项已于 2026-10-25 下线，改由 1Y AAA 同业存单收益率替代，见 ncd_rate.py；
   故本模块不再下载解析《货币政策执行报告》PDF。）
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


# ---------------------------------------------------------------- 7天逆回购利率


def _parse_7d_repo_rate(text: str) -> Optional[float]:
    """从《公开市场业务交易公告》正文里解析 7 天期逆回购操作利率。

    页面表格渲染成 "…期限 操作 利率 投标量 中标量 7 天 1. 40 % 1650 亿元…"，
    先去空白再匹配 `7天1.40%`。
    """
    flat = re.sub(r"\s+", "", text).replace("．", ".").replace("％", "%")
    flat = re.sub(r"(\d)\.(\d)", r"\1.\2", flat)
    if "逆回购" not in flat:
        return None
    m = re.search(r"7天(?:期)?[^0-9%]{0,24}?(\d+\.\d+)%", flat)
    if not m:
        m = re.search(r"7天(\d+\.\d+)%", flat)
    return float(m.group(1)) if m else None


def _announce_date(text: str) -> Optional[str]:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return "%s-%s-%s" % m.groups() if m else None


def fetch_reverse_repo_rate() -> Dict[str, Any]:
    """抓取 7 天期逆回购操作利率（取最近公告中最新一条含 7 天操作的）。

    并非每个交易日的公告都含 7 天期操作（可能只做隔夜、买断式或国债买卖），
    因此向前回溯若干条公告，取最新一条能解析出 7 天期利率的。
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
        rate = _parse_7d_repo_rate(text)
        if rate is None:
            continue
        return {
            "value": rate,
            "as_of": _announce_date(text) or "",
            "detail": {
                "公告": title,
                "来源": "中国人民银行公开市场业务交易公告",
                "链接": href if href.startswith("http") else (_working_base or _BASE_CANDIDATES[0]) + href,
            },
        }
    raise RuntimeError("最近公告中未找到 7 天期逆回购操作利率（%s）" % (last_error or "均无 7 天期操作"))


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


# ---------------------------------------------------------------- 信用 / 部门（分部门存贷款）
#
# ③「信用/部门」回答「钱在企业 / 住户 / 政府谁手里」，取《金融统计数据报告》第四、五节：
#   · 贷款分部门：「分部门看，住户贷款减少 1.03 万亿元，其中，短期贷款减少…，中长期贷款增加 1062 亿元；
#     企（事）业单位贷款增加 12.55 万亿元，其中，短期贷款…，中长期贷款…，票据融资…」
#   · 存款分部门：「其中，住户存款增加…，非金融企业存款增加…，财政性存款增加…，非银行业金融机构存款增加…」
#
# 口径都是**年初至今累计**（"前九个月人民币贷款增加…"），央行不披露当月增量，故：
#   当月增量   = 相邻两期累计相减（1 月直接取累计值）
#   同比多增   = 当月增量 − 去年同月增量
#   环比变动   = 当月增量 − 上月增量
# 这样能天然消掉春节、季末年末缴税等季节性 —— 也是 ③ 全部分项都用「同比多增 / 环比变动」的原因。
#
# 正文里同时出现存款方与贷款方两个列表（如"非银行业金融机构存款增加…"与
# "非银行业金融机构贷款减少…"），所以匹配式必须带上「存款」/「贷款」以免串档。

_AMOUNT_PATTERN = r"([\d.]+)\s*(万亿元|亿元)"

# 存款分部门：报告正文写法 → 内部键（统一折算成亿元）
_DEPOSIT_SPLIT_FIELDS = {
    "住户存款": "住户存款",
    "非金融企业存款": "企业存款",
    "财政性存款": "财政存款",
    "非银行业金融机构存款": "非银存款",
}

_REPORT_PERIODS = (
    (re.compile(r"(\d{4})\s*年\s*一季度"), 3),
    (re.compile(r"(\d{4})\s*年\s*上半年"), 6),
    (re.compile(r"(\d{4})\s*年\s*前三季度"), 9),
    (re.compile(r"(\d{4})\s*年\s*年度"), 12),
)

# 报告正文里的窗口描述：「前八个月 / 上半年 / 前三季度 / 一季度 / 8月份」
_REPORT_WINDOW = r"(前[一二三四五六七八九十]+个月|上半年|前三季度|前?一季度|\d{1,2}月份)"
_WINDOW_RE = re.compile(_REPORT_WINDOW)


def _report_period(title: str) -> Optional[str]:
    """由报告标题推出数据期：2026年8月→2026-08；上半年→2026-06；一季度→2026-03。

    季度/半年类的标题里没有「N 月」，_cn_ym_to_iso 会返回 None，故先按这些关键词归一；
    「2025年金融统计数据报告」这种只有年份的即全年（12 月）口径。
    """
    for pattern, month in _REPORT_PERIODS:
        m = pattern.search(title)
        if m:
            return "%s-%02d" % (m.group(1), month)
    period = _cn_ym_to_iso(title)
    if period:
        return period
    year = re.search(r"(\d{4})\s*年", title)
    return "%s-12" % year.group(1) if year else None


def _parse_yi(flat: str, name: str) -> Optional[float]:
    """解析「住户存款增加 6.99 万亿元」/「住户贷款减少 8271 亿元」→ 亿元（减少为负）。

    只取第一条匹配（同一分部门在同一句里只出现一次，且「合计」总在「其中」之前）。
    """
    pattern = re.compile(
        r"%s\s*(增加|减少|下降|上升)\s*%s" % (re.escape(name), _AMOUNT_PATTERN)
    )
    m = pattern.search(flat)
    if not m:
        return None
    value = float(m.group(2))
    if m.group(3) == "万亿元":
        value *= 10000.0
    sign = -1.0 if m.group(1) in ("减少", "下降") else 1.0
    return round(sign * value, 2)


def _parse_loan_split(flat: str) -> Dict[str, Dict[str, Optional[float]]]:
    """解析「分部门看，…」那句里的住户 / 企业贷款结构（累计口径，亿元）。

    返回 {"住户": {合计/短期/中长期/票据}, "企业": {...}}；解析不到返回空字典。
    住户也有「中长期贷款」（房贷主体）、企业也有（设备更新、厂房扩建），两者靠分段区分，
    不能全篇搜「中长期贷款」—— 那样只会命中住户那一条。
    """
    m = re.search(r"分部门看", flat)
    if not m:
        return {}
    tail = flat[m.end(): m.end() + 700]
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for seg in re.split(r"[；;]", tail):
        # 首句常带前导标点（"分部门看，住户贷款…"），先剥掉再判部门
        seg = seg.lstrip("，,：: 、")
        if not seg:
            continue
        if seg.startswith("住户"):
            key = "住户"
        elif seg.startswith("企"):
            key = "企业"
        else:  # 非银行业金融机构贷款：不进温度计，跳过
            continue
        out[key] = {
            "合计": _parse_yi(seg, "贷款"),
            "短期": _parse_yi(seg, "短期贷款"),
            "中长期": _parse_yi(seg, "中长期贷款"),
            "票据": _parse_yi(seg, "票据融资"),
        }
    return out


def _parse_report(title: str, flat: str) -> Optional[Dict[str, Any]]:
    """解析**单期**《金融统计数据报告》的分部门存贷款（累计口径，亿元）。"""
    loans = _parse_loan_split(flat)
    deposits = {label: _parse_yi(flat, needle) for needle, label in _DEPOSIT_SPLIT_FIELDS.items()}
    if not loans and all(v is None for v in deposits.values()):
        return None
    w = _WINDOW_RE.search(flat)
    return {
        "报告": title,
        "as_of": _report_period(title) or "",
        "期间": w.group(1) if w else "累计",
        "存款": deposits,
        "贷款": loans,
    }


def _shift_month(ym: Optional[str], months: int) -> Optional[str]:
    """'2026-01' 减 1 个月 → '2025-12'。"""
    if not ym or len(ym) < 7:
        return None
    try:
        year, month = int(ym[:4]), int(ym[5:7])
    except ValueError:
        return None
    total = year * 12 + (month - 1) + months
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def _cum_map(records: Dict[str, Dict[str, Any]], section: str, key: str,
             sub: Optional[str] = None) -> Dict[str, float]:
    """把各期累计读数抽成 {期间: 亿元}；sub 用于取「中长期贷款」这类子项。"""
    out: Dict[str, float] = {}
    for period, rec in records.items():
        node = (rec.get(section) or {}).get(key)
        if sub is not None:
            node = (node or {}).get(sub)
        if isinstance(node, (int, float)):
            out[period] = float(node)
    return out


def _monthly_of(cums: Dict[str, float], period: Optional[str]) -> Optional[float]:
    """累计 → 当月增量：1 月直接取累计值，其它月需相邻上期才可相减（缺期返回 None）。"""
    if not period or period not in cums:
        return None
    if int(period[5:7]) == 1:
        return cums[period]
    prev = _shift_month(period, -1)
    if prev in cums:
        return cums[period] - cums[prev]
    return None


def _yi_text(value: Optional[float]) -> str:
    return "—" if value is None else "%+.0f 亿" % value


def fetch_credit_structure(lookback: int = 24) -> Dict[str, Any]:
    """抓取《金融统计数据报告》，产出 ③ 信用/部门 三个分项的读数。

    只抓真正需要的 5 期报告（本期、上月、前月、去年同月、去年上月），
    先按标题解析出报告期再挑页面，避免把 20 多期正文全拉一遍。

    返回:
        {
          "as_of": "2026-09",
          "items": {
            "corp_ml_loan_yoy":   {value, as_of, detail},
            "household_ml_loan_yoy": {…},
            "fiscal_deposit_mom": {…},
          },
          "detail": {中文名: {子项: 显示文本}}   # 温度计「取数与口径」表用
        }
    """
    html = _get(COL_FIN_STAT).text
    candidates = _recent_links(html, "金融统计数据报告", limit=max(8, lookback))
    if not candidates:
        raise RuntimeError("《金融统计数据报告》栏目未解析到任何条目")

    by_period: Dict[str, Tuple[str, str]] = {}
    for title, href in candidates:
        period = _report_period(title)
        if period and period not in by_period:
            by_period[period] = (title, href)  # 栏目靠前＝更新，重复期取先出现的
    if not by_period:
        raise RuntimeError("《金融统计数据报告》栏目未解析出任何报告期")

    latest = max(by_period)
    need = [latest] + [_shift_month(latest, -n) for n in (1, 2, 12, 13)]

    records: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for period in need:
        if not period or period not in by_period:
            continue
        title, href = by_period[period]
        try:
            flat = re.sub(r"\s+", "", _page_text(href))
        except Exception as exc:
            errors.append("%s: %s" % (period, exc))
            continue
        rec = _parse_report(title, flat)
        if rec:
            records[period] = rec
    if not records:
        raise RuntimeError("分部门存贷款解析失败（%s）" % ("；".join(errors) or "正文无匹配字段"))

    latest = max(records)
    prev_m = _shift_month(latest, -1)
    prev2_m = _shift_month(latest, -2)
    yoy_m = _shift_month(latest, -12)
    yoy_prev_m = _shift_month(latest, -13)

    corp_cums = _cum_map(records, "贷款", "企业", "中长期")
    house_cums = _cum_map(records, "贷款", "住户", "中长期")
    fiscal_cums = _cum_map(records, "存款", "财政存款")

    def _yoy_diff(cums: Dict[str, float]) -> Tuple[Optional[float], Dict[str, Any]]:
        cur = _monthly_of(cums, latest)
        last = _monthly_of(cums, yoy_m)
        detail: Dict[str, Any] = {
            "本期当月": "%s（%s）" % (_yi_text(cur), latest),
            "去年同月": "%s（%s）" % (_yi_text(last), yoy_m or "—"),
        }
        value = None
        if cur is not None and last is not None:
            value = cur - last
            detail["同比多增"] = _yi_text(value)
        else:
            detail["同比多增"] = "—（缺去年同月数据）"
        return value, detail

    corp_yoy, corp_detail = _yoy_diff(corp_cums)
    house_yoy, house_detail = _yoy_diff(house_cums)

    # 财政存款：当月环比变动 = 当月 − 上月（都用累计相减得到当月）
    fiscal_cur = _monthly_of(fiscal_cums, latest)
    fiscal_prev = _monthly_of(fiscal_cums, prev_m)
    fiscal_mom = fiscal_cur - fiscal_prev if fiscal_cur is not None and fiscal_prev is not None else None
    fiscal_detail: Dict[str, Any] = {
        "本月变动": "%s（%s）" % (_yi_text(fiscal_cur), latest),
        "上月变动": "%s（%s）" % (_yi_text(fiscal_prev), prev_m or "—"),
        "环比变动": _yi_text(fiscal_mom),
    }

    # 辅助观测（不进打分）：企业短贷 / 票据冲量、住户短贷、非银存款
    corp_short = _monthly_of(_cum_map(records, "贷款", "企业", "短期"), latest)
    corp_bill = _monthly_of(_cum_map(records, "贷款", "企业", "票据"), latest)
    house_short = _monthly_of(_cum_map(records, "贷款", "住户", "短期"), latest)
    nonbank_dep = (records.get(latest, {}).get("存款") or {}).get("非银存款")

    items = {
        "corp_ml_loan_yoy": {"value": corp_yoy, "as_of": latest, "detail": corp_detail},
        "household_ml_loan_yoy": {"value": house_yoy, "as_of": latest, "detail": house_detail},
        "fiscal_deposit_mom": {"value": fiscal_mom, "as_of": latest, "detail": fiscal_detail},
    }
    for key, item in items.items():
        if item["value"] is not None:
            item["value"] = round(item["value"], 1)

    detail_out: Dict[str, Any] = {
        "企业中长期贷款（当月同比多增）": corp_detail,
        "住户中长期贷款（当月同比多增）": house_detail,
        "财政存款（当月环比变动）": fiscal_detail,
        "同比多增算法": "当月增量 = 相邻两期累计相减（1 月直接取累计）；同比多增 = 当月 − 去年同月",
        "辅助观测（不进打分）": {
            "企业短贷当月": _yi_text(corp_short),
            "票据融资当月": _yi_text(corp_bill),
            "住户短贷当月": _yi_text(house_short),
            "非银存款累计": _yi_text(nonbank_dep),
        },
        "报告期": "%s（%s）" % (latest, (records.get(latest) or {}).get("期间") or "累计"),
        "来源": "中国人民银行《金融统计数据报告》分部门存贷款",
    }
    if errors:
        detail_out["部分报告抓取失败"] = "；".join(errors)

    return {"as_of": latest, "items": items, "detail": detail_out,
            "periods": sorted(records.keys())}
