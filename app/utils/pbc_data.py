"""中国人民银行官网（www.pbc.gov.cn）数据抓取。

补齐 akshare 未覆盖的指标，改由央行官方发布渠道直接抓取：

======================  ==========================================  ==========
指标                    官方发布渠道                                 频率
======================  ==========================================  ==========
7天逆回购操作利率        《公开市场业务交易公告》                       每个交易日
社融存量同比             《金融统计数据报告》（数据解读栏目）             每月
超储率                   《中国货币政策执行报告》（PDF 正文）           每季度
分部门存贷款             《金融统计数据报告》第四、五节（累计口径）        每月
（住户/企业/非银/财政）
======================  ==========================================  ==========

设计约定：
- 每个 fetch_* 函数独立失败，异常由调用方按「该指标抓取失败」处理，不影响其他指标。
- 返回 {"value": float, "as_of": str, "detail": dict}；超储率在报告期未变时
  返回 {"skipped": True, ...} 以避免重复下载数 MB 的 PDF。
- 页面编码为 UTF-8；沙箱环境可能注入失效代理，故显式关闭 trust_env。
"""
import io
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
COL_MPR = "/zhengcehuobisi/125207/125227/125957/index.html"               # 货币政策执行报告

_HTML_TIMEOUT = 25
_PDF_TIMEOUT = 120


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


def fetch_deposit_loan_split() -> Dict[str, Any]:
    """抓取分部门存贷款累计增量（住户存款 / 住户贷款 / 非银存款 / 企业存款）。

    价值口径：**年初至今累计**（万亿元），与央行《金融统计数据报告》正文一致。
    返回 value = 住户贷款累计增量（判读「居民加杠杆还是缩表」的主口径），
    其余分项放在 detail 里供页面展开。
    """
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
        flat = re.sub(r"\s+", "", text)
        if "住户存款" not in flat:
            continue

        parsed: Dict[str, Optional[float]] = {}
        for needle, label in _DEPOSIT_SPLIT_FIELDS.items():
            parsed[label] = _parse_split_amount(flat, needle)

        household_loan = parsed.get("住户贷款")
        household_dep = parsed.get("住户存款")
        if household_loan is None and household_dep is None:
            continue

        as_of = _report_period(title) or _cn_ym_to_iso(flat) or ""
        window = re.search(r"(前[一二三四五六七八九十]+个月|上半年|前三季度|前?一季度)人民币存款增加", flat)
        return {
            "value": household_loan if household_loan is not None else 0.0,
            "as_of": as_of,
            "detail": {
                "报告": title,
                "住户存款": household_dep,
                "住户贷款": household_loan,
                "非银存款": parsed.get("非银存款"),
                "企业存款": parsed.get("企业存款"),
                "口径": "年初至今累计（%s）" % (window.group(1) if window else "累计"),
                "来源": "中国人民银行《金融统计数据报告》分部门存贷款",
            },
        }
    raise RuntimeError("未能解析分部门存贷款（%s）" % (last_error or "报告正文无匹配字段"))


# ---------------------------------------------------------------- 超储率（PDF）

_ER_PRIMARY = re.compile(
    r"(\d{1,2})\s*月末[^。；]{0,60}?超额(?:存款)?准备金率[为是约]\s*([0-9]+(?:\.[0-9]+)?)\s*%"
)
_ER_FALLBACK = re.compile(r"超额(?:存款)?准备金率[为是约]?\s*([0-9]+(?:\.[0-9]+)?)\s*%")

_QUARTER_CN = {"一": 3, "二": 6, "三": 9, "四": 12}


def _quarter_end_iso(title: str) -> Optional[str]:
    """从「2026年第二季度中国货币政策执行报告」推出报告期末月份。"""
    m = re.search(r"(\d{4})\s*年第([一二三四])季度", title)
    if m:
        return "%s-%02d" % (m.group(1), _QUARTER_CN[m.group(2)])
    return _cn_ym_to_iso(title)


def _pdf_text_pages(content: bytes, max_pages: int = 25):
    """逐页产出 PDF 文本（惰性，便于命中即停）。"""
    try:
        import pypdf
    except Exception as exc:
        raise RuntimeError("缺少 pypdf，无法解析《货币政策执行报告》PDF：%s" % exc)
    reader = pypdf.PdfReader(io.BytesIO(content))
    for page in reader.pages[:max_pages]:
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # 单页解析失败不影响整体
            logger.debug("PDF 单页解析失败: %s", exc)
            continue
        if text:
            yield text


def excess_reserve_from_report(title: str, href: str) -> Dict[str, Any]:
    """给定一份《中国货币政策执行报告》的标题与页面链接，下载 PDF 并提取超额准备金率。

    单期抓取，供两处共用，避免各写一套导致口径漂移：
    - fetch_excess_reserve()：只取最新一期，用于「最新快照」刷新
    - macro_backfill.series_excess_reserve()：逐期回溯，用于曲线历史

    解析失败抛异常，由调用方按「该期失败」处理。
    """
    page_html = _get(href).text
    pdfs = re.findall(r'href="([^"]+\.pdf)"', page_html, flags=re.I)
    if not pdfs:
        raise RuntimeError("报告页面未找到 PDF 附件：%s" % title)

    resp = _get(pdfs[0], timeout=_PDF_TIMEOUT)
    content = resp.content
    if not content[:4].startswith(b"%PDF"):
        raise RuntimeError("PDF 下载异常（返回内容非 PDF）")

    value = None
    month = None
    # 早年报告篇幅长、超储率可能在靠后章节，故放宽到 40 页（命中即停）
    for page_text in _pdf_text_pages(content, max_pages=40):
        flat = re.sub(r"\s+", "", page_text)
        m = _ER_PRIMARY.search(flat)
        if m:
            month = int(m.group(1))
            value = float(m.group(2))
            break
        m = _ER_FALLBACK.search(flat)
        if m:
            value = float(m.group(1))
            break
    if value is None:
        raise RuntimeError("未能在《%s》正文中提取到超额准备金率" % title)

    as_of = _quarter_end_iso(title) or ""
    if month and not as_of:
        year = re.search(r"(\d{4})\s*年", title)
        as_of = "%s-%02d" % (year.group(1), month) if year else as_of

    return {
        "value": value,
        "as_of": as_of,
        "detail": {
            "报告期": title,
            "来源": "中国人民银行《货币政策执行报告》",
            "口径": "季末金融机构超额准备金率",
            "as_of": as_of,
        },
    }


def fetch_excess_reserve(current_detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """抓取季度《中国货币政策执行报告》里的金融机构超额准备金率（最新一期）。

    报告为 1~4MB 的 PDF，且每季度才更新一次；若最新报告期与已存记录一致，
    直接返回 skipped，避免每次刷新都重复下载。逐期回溯见 macro_backfill。
    """
    html = _get(COL_MPR).text
    candidates = [
        (title, href) for title, href in _recent_links(html, "中国货币政策执行报告", limit=12)
        if "季度" in title
    ]
    if not candidates:
        raise RuntimeError("《货币政策执行报告》栏目未解析到季度报告条目")

    title, href = candidates[0]
    as_of = _quarter_end_iso(title) or ""

    # 报告期未变 -> 无需重复下载 PDF
    if current_detail and current_detail.get("报告期") == title and (
        current_detail.get("as_of") or as_of
    ):
        return {"skipped": True, "as_of": current_detail.get("as_of") or as_of}

    return excess_reserve_from_report(title, href)
