"""财务指标图表计算：基于已入库的 FinancialReport 记录，生成指标数值与同比/环比增速系列。

约定（来自产品需求）：
- 利润表 / 现金流量表为累计值：单季值 = 本季累计 − 上季累计（无上一期时近似等于累计值）
- 资产负债表为时点值：直接使用期末值
- 周转类指标使用期初期末平均余额，并按累计口径年化以保证各季度可比
- 去年同期 / 上期为 0 或负时，增速标记为 "—" 或 "扭亏"，不硬算百分比
- 货币类指标以亿元展示（保留 1 位小数），比率以 %、周转以 天 展示
"""
from typing import List, Optional

# 指标定义：(key, 名称, 展示单位, 增速类型 yoy/qoq, 口径类型 single/point/margin/turnover)
INDICATOR_DEFS = [
    ("revenue", "营业收入", "亿", "yoy", "single"),
    ("operating_cost", "营业成本", "亿", "yoy", "single"),
    ("gross_profit", "毛利", "亿", "yoy", "single"),
    ("net_profit", "归母净利润", "亿", "yoy", "single"),
    ("operating_cash_flow", "经营现金流净额", "亿", "yoy", "single"),
    ("total_cash", "现金总额", "亿", "yoy", "point"),
    ("inventory", "存货", "亿", "yoy", "point"),
    ("accounts_receivable", "应收账款", "亿", "yoy", "point"),
    ("contract_liabilities", "合同负债", "亿", "yoy", "point"),
    ("short_term_borrowing", "短期借款", "亿", "qoq", "point"),
    ("long_term_borrowing", "长期借款", "亿", "yoy", "point"),
    ("interest_expense", "利息支出", "亿", "qoq", "single"),
    ("gross_margin", "毛利率", "%", "yoy", "margin"),
    ("net_margin", "净利率", "%", "yoy", "margin"),
    ("inventory_turnover_days", "存货周转天数", "天", "yoy", "turnover"),
    ("receivables_turnover_days", "应收账款周转天数", "天", "yoy", "turnover"),
    ("interest_bearing_debt", "有息负债合计", "亿", "yoy", "point"),
]

# 有息负债合计 = 短期借款 + 一年内到期非流动负债 + 长期借款 + 应付债券
INTEREST_DEBT_FIELDS = (
    "short_term_borrowing",
    "non_current_liab_due_1y",
    "long_term_borrowing",
    "bonds_payable",
)


def _value(rec, attr: str) -> Optional[float]:
    v = getattr(rec, attr, None)
    return None if v is None else float(v)


def _quarter_no(date_str: str) -> int:
    """报告期 YYYY-MM-DD → 季度序数 1-4（03-31→1 ... 12-31→4）。"""
    month = int(date_str[5:7])
    return (month - 1) // 3 + 1


def _to_yi(yuan: Optional[float]) -> Optional[float]:
    """元 → 亿元，保留 1 位小数。"""
    return None if yuan is None else round(yuan / 1e8, 1)


def _same_quarter_last_year(records, i: int) -> Optional[int]:
    """返回去年同期（前一年同季度末）的记录下标；不存在则返回 None。"""
    date = records[i].report_date
    target_year = str(int(date[:4]) - 1)
    target_md = date[5:]
    for j in range(len(records)):
        if records[j].report_date[:4] == target_year and records[j].report_date[5:] == target_md:
            return j
    return None


def _growth_pct(cur: Optional[float], base: Optional[float]):
    """普通增速（%）：base 为 0/负时按约定返回标记，不硬算百分比。返回 (数值, 标记)。"""
    if cur is None or base is None:
        return None, None
    if base > 0:
        return round((cur - base) / base * 100, 1), None
    if base == 0:
        return None, "—"
    if cur > 0:
        return None, "扭亏"
    return None, "—"


def _growth_margin(cur: Optional[float], base: Optional[float]):
    """比率类增速（百分点差，如毛利率同比变化），始终可计算。返回 (数值, 标记)。"""
    if cur is None or base is None:
        return None, None
    return round(cur - base, 1), None


def compute_indicators(records, start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """计算全部指标系列。

    records: 按报告期升序的 FinancialReport ORM 列表（应取全量，保证单季还原 / 平均余额连续）。
    返回 {"periods": [...], "indicators": [...]}，periods 与各 series 长度一致。
    """
    n = len(records)
    dates = [r.report_date for r in records]

    # ---- 单季值（累计 → 单季）----
    # 国内季报利润表为「年初至本季末累计」，且每年年初重新累计：
    # Q1 本身即单季值；Q2-Q4 单季值 = 本季累计 − 上一季度累计（同一年内连续）。
    def single_series(attr: str) -> List[Optional[float]]:
        out = []
        for i in range(n):
            cum = _value(records[i], attr)
            if cum is None:
                out.append(None)
                continue
            if _quarter_no(dates[i]) == 1:
                out.append(cum)  # Q1 单季值 = 累计值
                continue
            prev = _value(records[i - 1], attr) if i > 0 else None
            out.append(None if prev is None else cum - prev)
        return out

    rev_q = single_series("revenue")
    cost_q = single_series("operating_cost")
    np_q = single_series("net_profit")
    ocf_q = single_series("operating_cash_flow")
    ie_q = single_series("interest_expense")
    gross_q = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(rev_q, cost_q)
    ]

    # ---- 时点值（资产负债表，直接用期末值）----
    def point_series(attr: str) -> List[Optional[float]]:
        return [_value(r, attr) for r in records]

    cash_pt = [
        (a + b) if (a is not None and b is not None) else None
        for a, b in zip(point_series("cash"), point_series("short_term_investment"))
    ]  # 现金总额 = 货币资金 + 短期理财（交易性金融资产）
    inventory_pt = point_series("inventory")
    receivables_pt = point_series("accounts_receivable")
    contract_pt = point_series("contract_liabilities")
    st_borrow_pt = point_series("short_term_borrowing")
    lt_borrow_pt = point_series("long_term_borrowing")
    ib_debt_pt = []
    for i in range(n):
        parts = [_value(records[i], f) for f in INTEREST_DEBT_FIELDS]
        ib_debt_pt.append(None if any(p is None for p in parts) else sum(parts))

    # ---- 比率 ----
    def ratio_series(numer: List[Optional[float]], denom: List[Optional[float]]) -> List[Optional[float]]:
        out = []
        for a, b in zip(numer, denom):
            if a is None or b is None or b == 0:
                out.append(None)
            else:
                out.append(a / b * 100)
        return out

    gross_margin = ratio_series(gross_q, rev_q)
    net_margin = ratio_series(np_q, rev_q)

    # ---- 周转天数（期初期末平均 + 累计年化）----
    def avg_prev_cur(series: List[Optional[float]]) -> List[Optional[float]]:
        out = [None]
        for i in range(1, n):
            a, b = series[i - 1], series[i]
            out.append(None if a is None or b is None else (a + b) / 2)
        return out

    def annualized(series: List[Optional[float]]) -> List[Optional[float]]:
        out = []
        for i in range(n):
            v = series[i]
            q = _quarter_no(dates[i])
            out.append(None if v is None or q == 0 else v / q * 4)
        return out

    ann_cost = annualized(point_series("operating_cost"))
    ann_rev = annualized(point_series("revenue"))
    inv_days = [
        None if (avg is None or ac is None or ac <= 0) else 365 * avg / ac
        for avg, ac in zip(avg_prev_cur(inventory_pt), ann_cost)
    ]
    rec_days = [
        None if (avg is None or ar is None or ar <= 0) else 365 * avg / ar
        for avg, ar in zip(avg_prev_cur(receivables_pt), ann_rev)
    ]

    value_map = {
        "revenue": rev_q,
        "operating_cost": cost_q,
        "gross_profit": gross_q,
        "net_profit": np_q,
        "operating_cash_flow": ocf_q,
        "interest_expense": ie_q,
        "total_cash": cash_pt,
        "inventory": inventory_pt,
        "accounts_receivable": receivables_pt,
        "contract_liabilities": contract_pt,
        "short_term_borrowing": st_borrow_pt,
        "long_term_borrowing": lt_borrow_pt,
        "interest_bearing_debt": ib_debt_pt,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "inventory_turnover_days": inv_days,
        "receivables_turnover_days": rec_days,
    }

    # 去年同期下标（同比基准）
    yoy_idx = [_same_quarter_last_year(records, i) for i in range(n)]

    indicators = []
    for key, name, unit, gtype, _kind in INDICATOR_DEFS:
        values = value_map[key]
        growth, labels = [], []
        for i in range(n):
            base = yoy_idx[i] if gtype == "yoy" else (i - 1 if i > 0 else None)
            bv = values[base] if base is not None else None
            g, lab = _growth_margin(values[i], bv) if unit == "%" else _growth_pct(values[i], bv)
            growth.append(g)
            labels.append(lab)

        def fmt(v):
            if v is None:
                return None
            return _to_yi(v) if unit == "亿" else round(v, 1)

        indicators.append({
            "key": key,
            "name": name,
            "unit": unit,
            "growth_type": gtype,
            "growth_name": "同比增速" if gtype == "yoy" else "环比增速",
            "values": [fmt(v) for v in values],
            "growth": growth,
            "labels": labels,
        })

    # 按 start/end 截取展示区间（计算基于全量，仅截取展示）
    idx = [i for i in range(n)
           if (not start or dates[i] >= start) and (not end or dates[i] <= end)]
    return {
        "periods": [dates[i] for i in idx],
        "indicators": [
            {
                **ind,
                "values": [ind["values"][i] for i in idx],
                "growth": [ind["growth"][i] for i in idx],
                "labels": [ind["labels"][i] for i in idx],
            }
            for ind in indicators
        ],
    }
