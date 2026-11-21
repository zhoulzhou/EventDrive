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

# 分数 → 状态标签（与用户给定的中文口径一致）
STATE_BANDS: Sequence[Tuple[float, str]] = (
    (20, "低迷"), (40, "偏冷"), (60, "中性"), (80, "偏热"), (101, "亢奋 · 风险区"),
)

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
        "source_note": "DR001 偏离与 R001−DR001 来自 akshare 银行间回购定盘利率；1Y AAA 同业存单收益率来自中国货币网「收盘收益率曲线 → 同业存单(AAA)」（中债估值口径）。三项均为日频，打分统一用 5 日滚动均值降噪。",
        "components": [
            {
                "key": "dr001_spread", "name": "DR001 − 政策利率（隔夜）", "weight": 0.50,
                "unit": "bp", "digits": 0, "from": "item",
                "anchors": [(-5, 100), (0, 90), (10, 72), (20, 52), (35, 28), (55, 8)],
                "anchor_text": "≤−5bp→100 · 0→90 · +10→72 · +20→52 · +35→28 · ≥+55→8",
            },
            {
                "key": "ncd_1y_aaa", "name": "1Y AAA 同业存单", "weight": 0.30,
                "unit": "%", "digits": 3, "from": "item",
                "anchors": [(1.00, 100), (1.30, 80), (1.60, 60), (1.95, 30), (2.30, 0)],
                "anchor_text": "≤1.00%→100 · 1.30→80 · 1.60→60 · 1.95→30 · ≥2.30→0",
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
                "anchors": [(-2, 15), (0, 40), (3, 60), (6, 80), (9, 100)],
                "anchor_text": "−2→15 · 0→40 · +3→60 · +6→80 · ≥+9→100",
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
            "三项全部来自央行《金融统计数据报告》的分部门存贷款：企业与住户中长贷"
            "「同比多增」= 当月增量（相邻两期累计相减）− 去年同月增量；财政存款为当月增量。"
        ),
        "components": [
            {
                "key": "corp_ml_yoy", "name": "企业中长贷 同比多增", "weight": 0.40,
                "unit": "亿", "digits": 0, "from": "item",
                "anchors": [(-4000, 0), (-2000, 25), (0, 50), (2000, 75), (4000, 100)],
                "anchor_text": "少增≥4000→0 · −2000→25 · 0→50 · +2000→75 · 多增≥4000→100",
            },
            {
                "key": "household_ml_yoy", "name": "住户中长贷 同比多增", "weight": 0.30,
                "unit": "亿", "digits": 0, "from": "item",
                "anchors": [(-3000, 0), (-1500, 25), (0, 50), (1500, 75), (3000, 100)],
                "anchor_text": "少增≥3000→0 · −1500→25 · 0→50 · +1500→75 · 多增≥3000→100",
            },
            {
                "key": "fiscal_deposit_mom", "name": "财政存款 当月增量", "weight": 0.30,
                "unit": "亿", "digits": 0, "from": "item",
                "anchors": [(-3000, 100), (-1500, 75), (0, 50), (1500, 25), (3000, 0)],
                "anchor_text": "减少≥3000→100 · −1500→75 · 0→50 · +1500→25 · 增加≥3000→0",
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
    "每月更新一次（金融数据发布后），三步走：① 取 10 项读数的最新值 → ② 按锚点表线性插值得单项分 → ③ 加权合成总分。",
]

READING_NOTE = (
    "判读阈值（比单看数字更重要）：<b>&lt;40</b> 偏冷区，加配债、防御；"
    "<b>40-60</b> 磨底区，结构行情，等信号；"
    "<b>≥60</b> 复苏确认区 —— 正好对应「反转三连」全部达标的合成分数"
    "（算例：社融回 8.5%、企业中长贷多增 2000 亿、住户中长贷转正 → 实体 58 + 信用 53 → 综合 ≈ 60.5，"
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


def state_of(score: Optional[float]) -> Optional[str]:
    """把 0-100 分数映射到中文状态标签（低迷 / 偏冷 / 中性 / 偏热 / 亢奋 · 风险区）。"""
    if score is None:
        return None
    for upper, label in STATE_BANDS:
        if score < upper:
            return label
    return STATE_BANDS[-1][1]



def _collect_metrics(items: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """收集分项读数与各自的数据日期，供打分表和页面「数据日期」列使用。"""
    metrics: Dict[str, Any] = {}
    meta: Dict[str, str] = {}
    for spec in SUB_INDICES:
        for comp in spec["components"]:
            item = items.get(comp["key"]) or {}
            metrics[comp["key"]] = item.get("value")
            if item.get("as_of"):
                meta[comp["key"]] = str(item["as_of"])
    return metrics, meta


def _credit_detail(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """取央行明细（三项信用分项同源），用于页面「取数与口径」说明。"""
    detail = ((items.get("corp_ml_yoy") or {}).get("detail") or {})
    if not detail:
        return {}
    keep = ("报告", "数据期", "企业中长期贷款", "住户中长期贷款", "财政性存款", "口径", "来源")
    return {k: detail[k] for k in keep if detail.get(k)}


def _sub_index(spec: Dict[str, Any], metrics: Dict[str, Any],
               meta: Dict[str, str]) -> Dict[str, Any]:
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
            "state": state_of(score),
            "zone": zone_of(score),
            "as_of": meta.get(comp["key"]),
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
        "state": state_of(score),
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
    """合成宏观温度计。items 为 build_items() 的产出（含 value / as_of / detail）。"""
    metrics, meta = _collect_metrics(items)
    subs = [_sub_index(spec, metrics, meta) for spec in SUB_INDICES]

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
                     "weight_pct": s["weight_pct"], "score": s["score"],
                     "state": s["state"], "zone": s["zone"]}
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
        "derived_detail": _credit_detail(items),
        "missing": [s["name"] for s in subs if s["score"] is None],
        "usage_steps": list(USAGE_STEPS),
        "reading_note": READING_NOTE,
        "spread_note": SPREAD_NOTE,
        "caveat_note": CAVEAT_NOTE,
    }
