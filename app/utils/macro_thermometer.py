"""宏观温度计：三个子温度计 → 加权合成 0-100 总分。

模型（口径由用户给定；**打分锚点一经确定不建议再改，否则历史不可比**）：

    宏观温度 = 资金面温度 × 25% + 实体热度温度 × 45% + 信用/部门温度 × 30%

- ① 资金面温度（松 = 高分）：宽松是政策友好 / 未来热度的燃料
- ② 实体热度温度（越高越热）
- ③ 信用 / 部门温度（资金活化 = 高分，空转 = 低分）

打分方式：每个分项按「两点间线性插值」映射到 0-100，超出锚点范围取端点分值；
子温度计内部按分项权重加权平均。某项缺数据时按「可用项权重归一化」处理并把缺失项
列出来 —— 单指标抓取失败不会让整根温度计作废。

另附「温差」= 资金面温度 − 信用/部门温度，可当第四个指标用：
≥30 → 空转加剧（政策暖但传导断），≤20 → 传导顺畅。

数据全部来自 macro_indicators.build_items() 的产出，不重复抓外部接口。
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 总分权重：资金面 25% / 实体热度 45% / 信用·部门 30%
COMPOSITE_WEIGHTS = {
    "liquidity": 0.25,
    "heat": 0.45,
    "structure": 0.30,
}

# 五段冷热配色（全站公共色，与 static/css/style.css 的 --zone-* 同一份，改色两边同步）
ZONE_COLORS = {
    "ice": "#5B8FC7",
    "cold": "#8BC8EA",
    "neutral": "#E4D48F",
    "warm": "#F4B393",
    "hot": "#EA6668",
}

# 分数 → 冷热段位（0-20 冰点 / 20-40 偏冷 / 40-60 中性 / 60-80 偏热 / 80-100 亢奋）
ZONE_BANDS: Sequence[Tuple[float, str]] = (
    (20, "ice"), (40, "cold"), (60, "neutral"), (80, "warm"), (101, "hot"),
)

# 「占新增存款比」的口径：
#   cumulative —— 年初至今累计（默认）。与央行报告披露口径一致，也是结构性判断
#                 「钱堆在资管还是在企业」的本意；当月口径受月末理财回表扰动极大。
#   monthly    —— 当月新增（相邻两期累计相减）。
DEPOSIT_SHARE_BASIS = "cumulative"

# ---------------------------------------------------------------- 锚点表
#
# anchors 为 [(读数, 分数)] 升序列表，两点间线性插值；超界取端点分值。
# 分数方向随指标含义而定（资金面：越紧分越低；实体：越热分越高；非银存款占比：越高分越低）。

SUB_INDICES: List[Dict[str, Any]] = [
    {
        "key": "liquidity",
        "name": "① 资金面温度",
        "weight": COMPOSITE_WEIGHTS["liquidity"],
        "direction": "松 = 高分（宽松是政策友好 / 未来热度的燃料）",
        "source_note": "DR007 偏离与 R001−DR001 来自 akshare 银行间回购定盘利率，超储率来自央行《货币政策执行报告》。",
        "components": [
            {
                "key": "dr007_spread", "name": "DR007 − 政策利率", "weight": 0.50,
                "unit": "bp", "digits": 0, "from": "item",
                "anchors": [(-20, 100), (0, 75), (20, 50), (50, 20), (100, 0)],
                "anchor_text": "≤−20bp→100 · 0→75 · +20→50 · +50→20 · ≥+100→0",
            },
            {
                "key": "excess_reserve", "name": "超储率", "weight": 0.30,
                "unit": "%", "digits": 2, "from": "item",
                "anchors": [(0.8, 10), (1.0, 40), (1.2, 60), (1.5, 80), (2.0, 100)],
                "anchor_text": "≥2.0%→100 · 1.5→80 · 1.2→60 · 1.0→40 · ≤0.8→10",
            },
            {
                "key": "r001_dr001", "name": "R001 − DR001", "weight": 0.20,
                "unit": "bp", "digits": 0, "from": "item",
                "anchors": [(10, 90), (20, 70), (40, 40), (60, 10)],
                "anchor_text": "≤10bp→90 · 20→70 · 40→40 · ≥60→10",
            },
        ],
    },
    {
        "key": "heat",
        "name": "② 实体热度温度",
        "weight": COMPOSITE_WEIGHTS["heat"],
        "direction": "越高越热（经济跑不跑得动）",
        "source_note": "PMI / PPI / M1、M2 同比来自 akshare（国家统计局口径），社融存量同比来自央行《金融统计数据报告》。",
        "components": [
            {
                "key": "pmi", "name": "制造业 PMI", "weight": 0.30,
                "unit": "", "digits": 1, "from": "item",
                "anchors": [(45, 0), (48, 40), (50, 55), (52, 75), (55, 100)],
                "anchor_text": "<45→0 · 48→40 · 50→55 · 52→75 · ≥55→100",
            },
            {
                "key": "ppi_yoy", "name": "PPI 同比", "weight": 0.25,
                "unit": "%", "digits": 1, "from": "item",
                "anchors": [(-2, 15), (0, 40), (2, 60), (4, 80), (6, 100)],
                "anchor_text": "−2→15 · 0→40 · +2→60 · +4→80 · ≥+6→100",
            },
            {
                "key": "tsf_yoy", "name": "社融存量同比", "weight": 0.25,
                "unit": "%", "digits": 1, "from": "item",
                "anchors": [(6, 0), (7, 25), (8, 45), (10, 65), (12, 85), (14, 100)],
                "anchor_text": "<6→0 · 7→25 · 8→45 · 10→65 · 12→85 · ≥14→100",
            },
            {
                "key": "m1_m2", "name": "M1 − M2 剪刀差", "weight": 0.20,
                "unit": "pp", "digits": 1, "from": "item",
                "anchors": [(-8, 0), (-5, 20), (-3, 40), (0, 60), (3, 80), (6, 100)],
                "anchor_text": "<−8→0 · −5→20 · −3→40 · 0→60 · +3→80 · ≥+6→100",
            },
        ],
    },
    {
        "key": "structure",
        "name": "③ 信用 / 部门温度",
        "weight": COMPOSITE_WEIGHTS["structure"],
        "direction": "资金活化 = 高分，空转 = 低分",
        "source_note": (
            "分部门存贷款来自央行《金融统计数据报告》第四、五节；"
            "「住户贷款当月增量」由相邻两期累计数相减得到，"
            "「企业 / 非银存款占新增存款比」按年初至今累计口径（分母为该期人民币存款新增额）。"
        ),
        "components": [
            {
                "key": "household_loan_mom", "name": "住户贷款当月增量", "weight": 0.40,
                "unit": "亿", "digits": 0, "from": "derived",
                "anchors": [(-3000, 0), (-2000, 20), (-1000, 40), (0, 60), (2000, 80), (5000, 100)],
                "anchor_text": "<−3000 亿→0 · −2000→20 · −1000→40 · 0→60 · +2000→80 · ≥+5000→100",
            },
            {
                "key": "corp_deposit_share", "name": "企业存款占新增存款比", "weight": 0.30,
                "unit": "%", "digits": 1, "from": "derived",
                "anchors": [(10, 20), (15, 40), (25, 60), (40, 80), (50, 100)],
                "anchor_text": "<10%→20 · 15%→40 · 25%→60 · 40%→80 · ≥50%→100",
            },
            {
                "key": "nonbank_deposit_share", "name": "非银存款占新增存款比", "weight": 0.30,
                "unit": "%", "digits": 1, "from": "derived",
                "anchors": [(5, 90), (10, 80), (20, 60), (30, 40), (40, 20)],
                "anchor_text": ">40%→20 · 30%→40 · 20%→60 · 10%→80 · <5%→90",
            },
        ],
    },
]

# 总分判读阈值（用户给定口径）
READING_BANDS: List[Dict[str, Any]] = [
    {"under": 40, "label": "偏冷区", "hint": "加配债、防御", "tone": "cold"},
    {"under": 60, "label": "磨底区", "hint": "结构行情，等信号", "tone": "neutral"},
    {"under": 75, "label": "复苏确认区", "hint": "跨过 60 就是升级信号", "tone": "warm"},
    {"under": 90, "label": "偏热区", "hint": "逐步降杠杆", "tone": "hot"},
    {"under": 101, "label": "过热区", "hint": "历史上对应 2015 / 2021 顶部，兑现", "tone": "hot"},
]

# 温差判读（资金面 − 信用/部门）：温差越大越说明「政策暖、传导断」
SPREAD_BANDS: List[Dict[str, Any]] = [
    {"under": 20, "label": "传导顺畅", "hint": "钱真正流到了企业和住户手里", "tone": "neutral"},
    {"under": 30, "label": "温差收敛中", "hint": "政策仍在暖、传导开始通", "tone": "warm"},
    {"under": 999, "label": "空转加剧", "hint": "政策暖但传导断，钱堆在资管", "tone": "hot"},
]

# 「怎么用这套温度计」——用户给定的使用说明，页面原样保留
USAGE_STEPS: List[str] = [
    "每月更新一次（金融数据发布后），三步走：① 填 10 个指标的最新值 → ② 按锚点表线性插值得单项分 → ③ 加权合成总分。",
]

READING_NOTE = (
    "判读阈值（比单看数字更重要）：<b>&lt;40</b> 偏冷区，加配债、防御；"
    "<b>40-60</b> 磨底区（当前 48），结构行情，等信号；"
    "<b>≥60</b> 复苏确认区 —— 正好对应「反转三连」全部达标的合成分数"
    "（算例：社融回 8.5%、住户贷款转正、企业存款占比回 25% → 实体 58 + 部门 53 → 综合 ≈ 60.5，"
    "<b>跨过 60 就是升级信号</b>）；<b>&gt;75</b> 偏热区，逐步降杠杆；"
    "<b>&gt;90</b> 过热区，历史上对应 2015 / 2021 顶部，兑现。"
)

SPREAD_NOTE = (
    "三个子温度分开看，比总分更有信息量：典型的「剪刀差结构」是资金面暖、部门温度冰"
    "（例如资金 74 vs 部门 25，温差 49 分），说明政策暖但传导断。"
    "温差收窄到 20 分以内，才说明钱真正流到了企业和住户手里。"
    "你可以把「温差」本身当第四个指标：<b>资金 − 部门 ≥ 30 → 空转加剧，≤ 20 → 传导顺畅</b>。"
)

CAVEAT_NOTE = (
    "这套锚点都是经验口径，你可以按自己的风险偏好调整权重"
    "（比如更看重实体热度就把 45% 提到 55%），但<b>打分锚点建议固定</b>，否则历史不可比。"
)


# ---------------------------------------------------------------- 打分

def zone_of(score: Optional[float]) -> Optional[str]:
    """把 0-100 分数映射到五段冷热色位（ice/cold/neutral/warm/hot）。"""
    if score is None:
        return None
    for upper, zone in ZONE_BANDS:
        if score < upper:
            return zone
    return "hot"


def lerp_score(value: Optional[float], anchors: Sequence[Tuple[float, float]]) -> Optional[float]:
    """两点间线性插值打分；超出锚点范围取端点分值（不外推）。"""
    if value is None or not anchors:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    pts = sorted(anchors, key=lambda p: p[0])
    if v <= pts[0][0]:
        return float(pts[0][1])
    if v >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return float(y1)
            ratio = (v - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    return float(pts[-1][1])


def _consecutive_months(current: Any, previous: Any) -> bool:
    """两期 as_of（YYYY-MM）是否为同一自然年内的相邻月 —— 才允许相减得当月增量。"""
    def _split(raw):
        text = str(raw or "")
        if len(text) < 7 or text[4] != "-":
            return None
        try:
            return int(text[:4]), int(text[5:7])
        except ValueError:
            return None

    cur, prev = _split(current), _split(previous)
    if not cur or not prev or cur[0] != prev[0]:
        return False
    return cur[1] - prev[1] == 1


def _derive_credit_metrics(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """从 ③ 的 pbc 明细里派生出温度计需要的三项读数。

    - 住户贷款当月增量（亿元）= 当期累计 − 上期累计
    - 企业 / 非银存款占新增存款比（%）= 分项累计 ÷ 该期人民币存款新增额

    明细不存在或口径不连续时返回 None，由上层按「该项缺数据」处理。
    """
    out: Dict[str, Any] = {
        "household_loan_mom": None,
        "corp_deposit_share": None,
        "nonbank_deposit_share": None,
        "_detail": {},
    }
    detail = ((items.get("household_dep_loan") or {}).get("detail") or {})
    split = detail.get("分部门明细") or {}
    if not split:
        return out

    prev = split.get("上期") or {}
    cur_loan, prev_loan = split.get("住户贷款"), prev.get("住户贷款")
    if cur_loan is not None and prev_loan is not None and _consecutive_months(
        split.get("as_of"), prev.get("as_of")
    ):
        # 万亿 → 亿元
        out["household_loan_mom"] = round((float(cur_loan) - float(prev_loan)) * 10000.0, 0)
        out["_detail"]["住户贷款当月增量"] = {
            "当期": "%s（%s）" % (cur_loan, split.get("as_of")),
            "上期": "%s（%s）" % (prev_loan, prev.get("as_of")),
        }

    total_cum = split.get("人民币存款增加")
    total = prev.get("人民币存款增加")
    if DEPOSIT_SHARE_BASIS == "monthly":
        total_flow = (
            float(total_cum) - float(total)
            if total_cum is not None and total is not None and _consecutive_months(
                split.get("as_of"), prev.get("as_of"))
            else None
        )
        basis_label = "当月新增"
    else:
        total_flow = float(total_cum) if total_cum is not None else None
        basis_label = "年初至今累计"

    corp, nonbank = split.get("企业存款"), split.get("非银存款")
    if total_flow:
        if corp is not None:
            out["corp_deposit_share"] = round(float(corp) / total_flow * 100.0, 1)
        if nonbank is not None:
            out["nonbank_deposit_share"] = round(float(nonbank) / total_flow * 100.0, 1)
        out["_detail"]["存款占比口径"] = basis_label
        out["_detail"]["分母(新增存款)"] = "%s 万亿元" % round(total_flow, 4)
    return out


def _collect_metrics(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for spec in SUB_INDICES:
        for comp in spec["components"]:
            if comp["from"] == "item":
                metrics[comp["key"]] = (items.get(comp["key"]) or {}).get("value")
    metrics.update(_derive_credit_metrics(items))
    return metrics


def _sub_index(spec: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """算一个子温度计：分项打分 + 按可用权重归一化。"""
    components = []
    used_weight = 0.0
    acc = 0.0
    for comp in spec["components"]:
        raw = metrics.get(comp["key"])
        score = lerp_score(raw, comp["anchors"])
        if score is not None:
            used_weight += comp["weight"]
            acc += score * comp["weight"]
        components.append({
            "key": comp["key"],
            "name": comp["name"],
            "weight": comp["weight"],
            "weight_pct": round(comp["weight"] * 100),
            "unit": comp["unit"],
            "digits": comp["digits"],
            "raw": raw,
            "score": None if score is None else round(score, 1),
            "zone": zone_of(score),
            "anchor_text": comp["anchor_text"],
            "anchors": [{"value": a, "score": b} for a, b in comp["anchors"]],
        })

    score = round(acc / used_weight, 1) if used_weight else None
    return {
        "key": spec["key"],
        "name": spec["name"],
        "weight": spec["weight"],
        "weight_pct": round(spec["weight"] * 100),
        "direction": spec["direction"],
        "source_note": spec["source_note"],
        "score": score,
        "zone": zone_of(score),
        "missing": [c["name"] for c in components if c["score"] is None],
        "components": components,
    }


def _reading_of(score: Optional[float]) -> Dict[str, Any]:
    if score is None:
        return {"label": "数据不足", "hint": "关键指标缺失，无法合成总分", "tone": "neutral"}
    for band in READING_BANDS:
        if score < band["under"]:
            return dict(band)
    return dict(READING_BANDS[-1])


def _spread_of(spread: Optional[float]) -> Dict[str, Any]:
    if spread is None:
        return {"label": "数据不足", "hint": "资金面或部门温度缺失", "tone": "neutral"}
    for band in SPREAD_BANDS:
        if spread < band["under"]:
            return dict(band)
    return dict(SPREAD_BANDS[-1])


def compute_thermometer(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """合成宏观温度计。items 为 build_items() 的产出（含 value / detail）。"""
    metrics = _collect_metrics(items)
    subs = [_sub_index(spec, metrics) for spec in SUB_INDICES]

    available = {s["key"]: s["score"] for s in subs if s["score"] is not None}
    used_weight = sum(COMPOSITE_WEIGHTS[k] for k in available)
    score = (
        round(sum(available[k] * COMPOSITE_WEIGHTS[k] for k in available) / used_weight, 1)
        if used_weight else None
    )

    liq = available.get("liquidity")
    struc = available.get("structure")
    spread = round(liq - struc, 1) if liq is not None and struc is not None else None

    reading = _reading_of(score)
    spread_info = _spread_of(spread)

    return {
        "score": score,
        "zone": zone_of(score),
        "reading": reading["label"],
        "reading_hint": reading["hint"],
        "reading_tone": reading["tone"],
        "formula": "资金面温度 × 25% + 实体热度温度 × 45% + 信用/部门温度 × 30%",
        "weights": [{"key": s["key"], "name": s["name"], "weight": s["weight"],
                     "weight_pct": s["weight_pct"], "score": s["score"], "zone": s["zone"]}
                    for s in subs],
        "subs": subs,
        "spread": spread,
        "spread_label": spread_info["label"],
        "spread_hint": spread_info["hint"],
        "spread_tone": spread_info["tone"],
        "spread_bands": [dict(b) for b in SPREAD_BANDS],
        "reading_bands": [dict(b) for b in READING_BANDS],
        "zone_colors": dict(ZONE_COLORS),
        "zone_bands": [{"under": u, "zone": z} for u, z in ZONE_BANDS],
        "deposit_share_basis": DEPOSIT_SHARE_BASIS,
        "derived_detail": metrics.get("_detail") or {},
        "missing": [s["name"] for s in subs if s["score"] is None],
        "usage_steps": list(USAGE_STEPS),
        "reading_note": READING_NOTE,
        "spread_note": SPREAD_NOTE,
        "caveat_note": CAVEAT_NOTE,
    }
