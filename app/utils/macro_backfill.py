"""市场指标历史数据回溯补齐。

实时抓取只能拿到「最新一期」读数，而页面上的趋势图需要成序列的历史数据。
本模块把各数据源可回溯的历史一次性拉成序列，写进 macro_indicator_history。

数据源与实时抓取完全一致（app/utils/macro_indicators.py + app/utils/pbc_data.py），
区别只是把「取最后一行」换成「取整段区间」：

======================  ==========================================  ==================
指标                    数据源                                       回溯范围
======================  ==========================================  ==================
DR007 偏离              akshare repo_rate_hist（FDR007）             逐月回溯（默认 24 个月）
R001 − DR001            akshare repo_rate_hist（FR001/FDR001）      逐月回溯（默认 24 个月）
制造业 PMI              akshare macro_china_pmi                      全量（默认取近 60 期）
PPI 同比                akshare macro_china_ppi                      全量（默认取近 60 期）
M1 / M2 同比            akshare macro_china_money_supply             全量（默认取近 60 期）
社融存量同比            央行《金融统计数据报告》逐期解析               栏目可列出的最近 13 期
======================  ==========================================  ==================

两处需要注意的口径问题：

1. **DR007 偏离的锚随时间变化**：7 天期逆回购操作利率是政策利率，历史上多次调整，
   直接用当前 1.40% 去算几年前的数据会整体偏移。故按 POLICY_RATE_STEPS 取「当日
   生效的政策利率」作为锚。
2. **repo_rate_hist 单次查询区间不能超过一个月**，因此按自然月逐段请求再拼接。

本模块只负责「产出记录」，落库由 crud.bulk_upsert_macro_history 完成；
任一段抓取失败只跳过该段，不影响其余数据。
"""
import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 7 天期逆回购操作利率历史（政策利率锚，单位 %）。
# 按「生效日」倒序排列，取第一个 start <= 查询日 的档位。
POLICY_RATE_STEPS: List[Tuple[str, float]] = [
    ("2025-05-08", 1.40),   # 1.50% → 1.40%
    ("2024-09-27", 1.50),   # 1.70% → 1.50%
    ("2024-07-22", 1.70),   # 1.80% → 1.70%（同时改为固定利率、数量招标）
    ("2023-08-15", 1.80),   # 1.90% → 1.80%
    ("2023-06-13", 1.90),   # 2.00% → 1.90%
    ("2000-01-01", 2.00),
]

# 央行《金融统计数据报告》标题里的报告期标记
_PERIOD_TOKENS = [
    ("上半年", "06"),
    ("前三季度", "09"),
    ("一季度", "03"),
    ("二季度", "06"),
    ("三季度", "09"),
    ("四季度", "12"),
]


def policy_rate_on(iso_date: str) -> float:
    """返回某日生效的 7 天期逆回购操作利率（%）。"""
    d = (iso_date or "")[:10]
    for start, rate in POLICY_RATE_STEPS:
        if d >= start:
            return rate
    return POLICY_RATE_STEPS[-1][1]


def _num(value: Any) -> Optional[float]:
    """安全转 float：None / NaN / 非数值统一返回 None。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _import_akshare():
    try:
        import akshare as ak  # noqa: F401
        return ak
    except Exception as exc:  # pragma: no cover - 环境相关
        logger.warning("akshare 不可用，无法回溯历史: %s", exc)
        return None


def _month_windows(months: int) -> List[Tuple[str, str]]:
    """生成最近 months 个自然月的 (起, 止) 区间（YYYYMMDD），按时间正序。

    repo_rate_hist 单次查询区间不能跨月，故按自然月切分；当月截止到今天。
    """
    today = date.today()
    windows: List[Tuple[str, str]] = []
    y, m = today.year, today.month
    for _ in range(max(1, months)):
        first = date(y, m, 1)
        if (y, m) == (today.year, today.month):
            last = today
        else:
            nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            last = nxt - timedelta(days=1)
        windows.append((first.strftime("%Y%m%d"), last.strftime("%Y%m%d")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    windows.reverse()
    return windows


def _rec(as_of: str, value: float, source: str, detail: Optional[dict] = None) -> Dict[str, Any]:
    return {
        "key": None,  # 由调用方填充
        "value": value,
        "as_of": as_of,
        "source": source,
        "detail": detail or {},
    }


# ---------------------------------------------------------------- 日度：回购利率


def series_repo_daily(months: int = 24) -> Dict[str, List[Dict[str, Any]]]:
    """逐月回溯 DR007 偏离与 R001 − DR001（日度）。

    返回 {"dr007_spread": [...], "r001_dr001": [...]}，按时间正序。
    某个月请求失败只跳过该月。
    """
    ak = _import_akshare()
    if ak is None:
        raise RuntimeError("当前运行环境未安装 akshare，无法回溯历史")

    out: Dict[str, List[Dict[str, Any]]] = {"dr007_spread": [], "r001_dr001": []}
    ok_months = 0
    for start, end in _month_windows(months):
        try:
            df = ak.repo_rate_hist(start_date=start, end_date=end)
        except Exception as exc:
            logger.warning("回购利率回溯失败 %s~%s: %s", start, end, exc)
            continue
        if df is None or df.empty or "date" not in getattr(df, "columns", []):
            continue
        ok_months += 1
        for _, row in df.dropna(subset=["date"]).iterrows():
            day = str(row["date"])[:10]
            fdr007 = _num(row.get("FDR007"))
            fdr001 = _num(row.get("FDR001"))
            fr001 = _num(row.get("FR001"))

            if fdr007 is not None:
                anchor = policy_rate_on(day)
                rec = _rec(day, round((fdr007 - anchor) * 100, 1), "akshare",
                           {"DR007": fdr007, "逆回购利率": anchor})
                rec["key"] = "dr007_spread"
                out["dr007_spread"].append(rec)

            if fdr001 is not None and fr001 is not None:
                rec = _rec(day, round((fr001 - fdr001) * 100, 1), "akshare",
                           {"R001": fr001, "DR001": fdr001})
                rec["key"] = "r001_dr001"
                out["r001_dr001"].append(rec)

    logger.info("回购利率回溯完成: %d/%d 个月有数据, DR007 偏离 %d 条, R001−DR001 %d 条",
                ok_months, months, len(out["dr007_spread"]), len(out["r001_dr001"]))
    return out


# ---------------------------------------------------------------- 月度：akshare


def _month_iso(raw: Any) -> str:
    """把 "2026年08月份" 统一成 "2026-08"。"""
    text = str(raw).strip()
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})", text)
    if m:
        return "%s-%02d" % (m.group(1), int(m.group(2)))
    m = re.search(r"(\d{4})-?(\d{2})", text)
    if m:
        return "%s-%s" % (m.group(1), m.group(2))
    return text


def _tail(rows: List[Dict[str, Any]], months: int) -> List[Dict[str, Any]]:
    rows.sort(key=lambda r: r["as_of"])
    return rows[-months:] if months and months > 0 else rows


def series_monthly(months: int = 60) -> Dict[str, List[Dict[str, Any]]]:
    """回溯月度序列：PMI、PPI 同比、M1/M2 同比、M1 − M2 剪刀差。

    返回 {key: [...]}，按时间正序。单项失败不影响其余。
    """
    ak = _import_akshare()
    if ak is None:
        raise RuntimeError("当前运行环境未安装 akshare，无法回溯历史")

    out: Dict[str, List[Dict[str, Any]]] = {
        "pmi": [], "ppi_yoy": [], "m1_yoy": [], "m2_yoy": [], "m1_m2": [],
    }

    def _fail(name: str, exc: Exception):
        logger.warning("月度序列回溯失败 [%s]: %s", name, exc)

    # 制造业 PMI
    try:
        df = ak.macro_china_pmi()
        for _, row in df.iterrows():
            v = _num(row.get("制造业-指数"))
            if v is None:
                continue
            out["pmi"].append({"key": "pmi", "value": v, "as_of": _month_iso(row["月份"]),
                               "source": "akshare", "detail": {}})
    except Exception as exc:
        _fail("pmi", exc)

    # PPI 当月同比
    try:
        df = ak.macro_china_ppi()
        for _, row in df.iterrows():
            v = _num(row.get("当月同比增长"))
            if v is None:
                continue
            out["ppi_yoy"].append({"key": "ppi_yoy", "value": v, "as_of": _month_iso(row["月份"]),
                                   "source": "akshare", "detail": {}})
    except Exception as exc:
        _fail("ppi_yoy", exc)

    # M1 / M2 同比 + 剪刀差
    try:
        df = ak.macro_china_money_supply()
        for _, row in df.iterrows():
            m1 = _num(row.get("货币(M1)-同比增长"))
            m2 = _num(row.get("货币和准货币(M2)-同比增长"))
            ym = _month_iso(row["月份"])
            if m1 is not None:
                out["m1_yoy"].append({"key": "m1_yoy", "value": m1, "as_of": ym,
                                      "source": "akshare", "detail": {}})
            if m2 is not None:
                out["m2_yoy"].append({"key": "m2_yoy", "value": m2, "as_of": ym,
                                      "source": "akshare", "detail": {}})
            if m1 is not None and m2 is not None:
                out["m1_m2"].append({"key": "m1_m2", "value": round(m1 - m2, 2), "as_of": ym,
                                     "source": "akshare", "detail": {"M1同比": m1, "M2同比": m2}})
    except Exception as exc:
        _fail("m1_m2", exc)

    return {k: _tail(v, months) for k, v in out.items()}


# ---------------------------------------------------------------- 月度：央行社融


def _report_period(title: str) -> str:
    """从《金融统计数据报告》标题推出报告期（YYYY-MM）。

    标题形如「2026年8月金融统计数据报告」「2026年上半年金融统计数据报告」
    「2026年前三季度金融统计数据报告」「2025年金融统计数据报告」。
    """
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", title)
    if m:
        return "%s-%02d" % (m.group(1), int(m.group(2)))
    year_m = re.search(r"(\d{4})\s*年", title)
    if not year_m:
        return ""
    year = year_m.group(1)
    for token, month in _PERIOD_TOKENS:
        if token in title:
            return "%s-%s" % (year, month)
    # 只有年份（即全年/12 月口径）
    return "%s-12" % year


def series_tsf(limit: int = 13) -> List[Dict[str, Any]]:
    """逐期解析央行《金融统计数据报告》，回溯社融存量同比。

    栏目索引页一次列出最近 13 期报告（含季度/半年度口径），每期一次页面请求。
    """
    from app.utils import pbc_data

    html = pbc_data._get(pbc_data.COL_FIN_STAT).text
    candidates = pbc_data._recent_links(html, "金融统计数据报告", limit=max(1, limit))
    if not candidates:
        raise RuntimeError("《金融统计数据报告》栏目未解析到任何条目")

    rows: List[Dict[str, Any]] = []
    for title, href in candidates:
        as_of = _report_period(title)
        if not as_of:
            continue
        try:
            text = pbc_data._page_text(href)
        except Exception as exc:
            logger.warning("社融回溯：报告抓取失败 %s: %s", title, exc)
            continue
        m = pbc_data._TSF_PATTERN.search(text)
        if not m:
            logger.debug("社融回溯：报告未匹配到存量同比 %s", title)
            continue
        rows.append({
            "key": "tsf_yoy",
            "value": float(m.group(2)),
            "as_of": as_of,
            "source": "pbc",
            "detail": {"报告": title, "存量": "%s 万亿元" % m.group(1)},
        })

    # 同一报告期可能有多条（如「2026年上半年」与「2026年6月」），保留先出现的（栏目更靠前＝更新）
    seen = set()
    deduped = []
    for r in sorted(rows, key=lambda r: r["as_of"]):
        if r["as_of"] in seen:
            continue
        seen.add(r["as_of"])
        deduped.append(r)
    logger.info("社融存量同比回溯完成: %d 期", len(deduped))
    return deduped


# ---------------------------------------------------------------- 季度：央行超储率


def series_excess_reserve(limit: int = 20) -> List[Dict[str, Any]]:
    """逐期解析央行《货币政策执行报告》PDF，回溯季末金融机构超额准备金率。

    超储率只在季报正文里以文字给出，每期要下载一份 1~4MB 的 PDF（约 5~10 秒），
    所以 limit 控制回溯的季度数（20 期≈5 年，约 2 分钟），不适合每天全量跑。
    单期解析失败只跳过该期，不影响其余——早年报告的排版差异会体现在这里。
    """
    from app.utils import pbc_data

    html = pbc_data._get(pbc_data.COL_MPR).text
    links = pbc_data._recent_links(
        html, "中国货币政策执行报告", limit=max(12, limit + 10)
    )
    quarters = [(t, h) for t, h in links if "季度" in t][: max(1, limit)]
    if not quarters:
        raise RuntimeError("《货币政策执行报告》栏目未解析到季度条目")

    rows: List[Dict[str, Any]] = []
    for title, href in quarters:
        as_of = pbc_data._quarter_end_iso(title)
        if not as_of:
            continue
        try:
            item = pbc_data.excess_reserve_from_report(title, href)
        except Exception as exc:
            logger.warning("超储率回溯：解析失败 %s: %s", title, exc)
            continue
        rows.append({
            "key": "excess_reserve",
            "value": item["value"],
            "as_of": item.get("as_of") or as_of,
            "source": "pbc",
            "detail": item.get("detail") or {},
        })

    # rows 已按栏目顺序（新 → 旧），同一季度若出现多条则保留更靠前＝更新的那条
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for rec in rows:
        if rec["as_of"] in seen:
            continue
        seen.add(rec["as_of"])
        deduped.append(rec)
    deduped.sort(key=lambda rec: rec["as_of"])
    logger.info("超储率回溯完成: %d 期", len(deduped))
    return deduped


# ---------------------------------------------------------------- 汇总


def backfill_all(months_daily: int = 24, months_monthly: int = 60,
                 tsf_limit: int = 13, er_limit: int = 0) -> Dict[str, Any]:
    """执行全量回溯，返回 {series: {key: [...]}, counts, errors, anchor_steps}。

    er_limit > 0 时额外回溯超储率（每期一份央行季报 PDF，较慢），默认 0 跳过。
    """
    series: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}

    for label, loader in (
        ("daily", lambda: series_repo_daily(months_daily)),
        ("monthly", lambda: series_monthly(months_monthly)),
    ):
        try:
            series.update(loader())
        except Exception as exc:
            errors[label] = str(exc)
            logger.error("回溯失败 [%s]: %s", label, exc)

    try:
        series["tsf_yoy"] = series_tsf(tsf_limit)
    except Exception as exc:
        errors["tsf_yoy"] = str(exc)
        logger.error("回溯失败 [tsf_yoy]: %s", exc)

    # 超储率要逐份下载央行季报 PDF，较慢：由调用方按需开启（er_limit=0 即跳过）
    if er_limit and er_limit > 0:
        try:
            series["excess_reserve"] = series_excess_reserve(er_limit)
        except Exception as exc:
            errors["excess_reserve"] = str(exc)
            logger.error("回溯失败 [excess_reserve]: %s", exc)

    counts = {k: len(v) for k, v in series.items()}
    return {"series": series, "counts": counts, "errors": errors,
            "anchor_steps": [{"from": d, "rate": r} for d, r in POLICY_RATE_STEPS]}
