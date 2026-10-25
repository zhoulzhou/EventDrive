"""宏观温度计：三个子温度计 → 加权合成 0-100 总分。

模型（口径由用户给定；**打分锚点一经确定不建议再改，否则历史不可比**）：

    宏观温度 = 资金面温度 × 25% + 实体热度温度 × 45% + 信用/部门温度 × 30%

- ① 资金面温度（松 = 高分，25%）：钱贵不贵 —— 银行间流动性、银行负债供给
      DR007 偏离 50% + 1Y AAA 同业存单 30% + R001−DR001 分层 20%
      （2026-10-25：原「超储率」位置由 1Y AAA 同业存单替代 —— 季频且需解析季报 PDF，
        滚动刷新体验差；NCD 是日频，直接反映银行中期负债成本）
- ② 实体热度温度（越高越热，45%，主线模块）：经济跑不跑
      PMI 30% + PPI 25% + 社融存量同比 25% + M1−M2 剪刀差 20%
- ③ 信用/部门温度（30%）：钱在谁手里
      企业中长贷同比多增 40% + 住户中长贷同比多增 30% + 财政存款当月环比 30%

打分方式（统一规则）：
1. 单项 0-100 分，越高 = 越宽松 / 越扩张 / 越景气；
2. 两点间**线性插值**，读数超出端点阈值直接取边界分数（不外推）；
3. 利率类分项优先用 **5 日滚动均值** 打分，规避单日缴税、跨季、月末脉冲；
4. 某项缺数据时按「可用项权重归一化」处理并把缺失项列出来 —— 单指标抓取失败
   不会让整根温度计作废。

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

# 段位 → 中文状态标签（与股市热度页 TEMP_ZONES 的措辞保持一致，便于横向对照阅读）
ZONE_LABELS: Dict[str, str] = {
    "ice": "低迷",
    "cold": "偏冷",
    "neutral": "中性",
    "warm": "偏热",
    "hot": "亢奋 · 风险区",
}

# ---------------------------------------------------------------- 锚点表
#
# anchors 为 [(读数, 分数)] 升序列表，两点间线性插值；超界取端点分值。
# 分数方向随指标含义而定（资金面：越紧分越低；实体：越热分越高；财政存款：越投放分越高）。

SUB_INDICES: List[Dict[str, Any]] = [
    {
        "key": "liquidity",
        "name": "① 资金面温度",
        "weight": COMPOSITE_WEIGHTS["liquidity"],
        "direction": "松 = 高分（钱贵不贵：银行间流动性 + 银行负债供给）",
        "source_note": (
            "DR007 偏离与 R001−DR001 来自 akshare 银行间回购定盘利率；"
            "1Y AAA 同业存单收益率来自中国货币网「收盘收益率曲线 → 同业存单(AAA)」（中债估值口径）。"
            "三项均为日频，打分统一用 5 日滚动均值降噪。"
        ),
        "components": [
            {
                "key": "dr007_spread", "name": "DR007 − 政策利率", "weight": 0.50,
                "unit": "bp", "digits": 0, "from": "item",
                "anchors": [(-20, 100), (0, 75), (20, 50), (50, 20), (100, 0)],
                "anchor_text": "≤−20bp→100 · 0→75 · +20→50 · +50→20 · ≥+100→0",
            },
            {
                "key": "ncd_1y_aaa", "name": "1Y AAA 同业存单", "weight": 0.30,
                "unit": "%", "digits": 3, "from": "item",
                # 2026-10-25 按低利率环境校准（原 2.00~3.00 是老中枢口径，当前读数会恒贴 100 分饱和）
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
        "source_note": (
            "PMI / PPI / M1、M2 同比来自 akshare（国家统计局口径），社融存量同比来自央行《金融统计数据报告》。"
            "PMI 打分只用 headline，新订单分项作为辅助观测（本页未接入、不进打分）。"
        ),
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
        "direction": "钱在谁手里：企业扩产 / 居民加杠杆 / 财政投放 = 高分",
        "source_note": (
            "三项全部来自央行《金融统计数据报告》的分部门存贷款，原始口径是年初至今累计，"
            "本页折算成当月增量后与去年同月（或上月）比较，天然消除春节、季末年末的季节性。"
            "企业短贷与票据冲量不算资本开支，只作辅助观测、不进打分。"
        ),
        "components": [
            {
                "key": "corp_ml_loan_yoy", "name": "企业中长贷 同比多增", "weight": 0.40,
                "unit": "亿", "digits": 0, "from": "item",
                "anchors": [(-4000, 0), (-2000, 25), (0, 50), (2000, 75), (4000, 100)],
                "anchor_text": "少增≥4000→0 · −2000→25 · 0→50 · +2000→75 · 多增≥4000→100",
            },
            {
                "key": "household_ml_loan_yoy", "name": "住户中长贷 同比多增", "weight": 0.30,
                "unit": "亿", "digits": 0, "from": "item",
                "anchors": [(-3000, 0), (-1500, 25), (0, 50), (1500, 75), (3000, 100)],
                "anchor_text": "少增≥3000→0 · −1500→25 · 0→50 · +1500→75 · 多增≥3000→100",
            },
            {
                "key": "fiscal_deposit_mom", "name": "财政存款 当月环比", "weight": 0.30,
                "unit": "亿", "digits": 0, "from": "item",
                # 方向相反：财政存款减少＝国库拨付、投放实体 → 高分
                "anchors": [(-3000, 100), (-1500, 75), (0, 50), (1500, 25), (3000, 0)],
                "anchor_text": "减少≥3000→100 · −1500→75 · 0→50 · +1500→25 · 增加≥3000→0",
            },
        ],
    },
]

# 三项信用分项在落库 detail 里附带的「取数与口径」明细键
CREDIT_ITEM_KEYS = ("corp_ml_loan_yoy", "household_ml_loan_yoy", "fiscal_deposit_mom")

# 总分判读阈值（沿用既有口径：0-20 低迷 / 20-40 偏冷 / 40-60 中性 / 60-80 偏热 / 80-100 亢奋）
READING_BANDS: List[Dict[str, Any]] = [
    {"under": 20, "label": "低迷", "hint": "各项均在收缩，防御为主", "tone": "ice"},
    {"under": 40, "label": "偏冷", "hint": "加配债、防御", "tone": "cold"},
    {"under": 60, "label": "中性", "hint": "结构行情，等信号", "tone": "neutral"},
    {"under": 80, "label": "偏热", "hint": "景气扩张，逐步降杠杆", "tone": "warm"},
    {"under": 101, "label": "亢奋 · 风险区", "hint": "历史对应阶段顶部，兑现", "tone": "hot"},
]

# 温差判读（资金面 − 信用/部门）：温差越大越说明「政策暖、传导断」
SPREAD_BANDS: List[Dict[str, Any]] = [
    {"under": 20, "label": "传导顺畅", "hint": "钱真正流到了企业和住户手里", "tone": "neutral"},
    {"under": 30, "label": "温差收敛中", "hint": "政策仍在暖、传导开始通", "tone": "warm"},
    {"under": 999, "label": "空转加剧", "hint": "政策暖但传导断，钱堆在资管", "tone": "hot"},
]

# 「怎么用这套温度计」——页面原样展示
USAGE_STEPS: List[str] = [
    "每个交易日看资金面（日频），每月中旬金融数据发布后看实体热度与信用/部门（月频）。",
    "三步走：① 取各分项最新读数（利率类取 5 日均） → ② 按锚点表线性插值得单项分 → ③ 按权重加权合成。",
    "只看总分不够，三个子温度计分开读：谁在拉动、谁在拖后腿，比总分更有信息量。",
]

READING_NOTE = (
    "判读阈值（比单看数字更重要）：<b>0-20 低迷</b>，各项均收缩；"
    "<b>20-40 偏冷</b>，加配债、防御；<b>40-60 中性</b>，结构行情，等信号；"
    "<b>60-80 偏热</b>，景气扩张、逐步降杠杆；<b>80-100 亢奋·风险区</b>，"
    "历史上多对应阶段顶部，兑现为上。"
)

SPREAD_NOTE = (
    "三个子温度分开看，比总分更有信息量：典型的「剪刀差结构」是资金面暖、信用部门冰，"
    "说明政策暖但传导断 —— 钱在银行间和资管里转，没进企业资本开支和居民房贷。"
    "温差收窄到 20 分以内，才说明钱真正流到了企业和住户手里。"
    "你可以把「温差」本身当第四个指标：<b>资金 − 信用部门 ≥ 30 → 空转加剧，≤ 20 → 传导顺畅</b>。"
)

CAVEAT_NOTE = (
    "这套锚点都是经验口径，你可以按自己的风险偏好调整<b>权重</b>，"
    "但<b>打分锚点建议固定</b>，否则历史不可比。"
    "⚠ 资金面里 1Y 存单的锚点已于 2026-10-25 整体下移到 <b>1.00%~2.30%</b>"
    "（旧口径 2.00%~3.00% 是按 2023-2024 年中枢定的，当前读数长期低于 2.00% 会使该项恒取 100 分、失去区分度）。"
    "新口径下当前读数落在插值区间内部、能反映边际变化；"
    "代价是利率明显上行时该项较早触底（&gt;2.30% 恒取 0 分），届时需随中枢上移一起复核。"
)


# ---------------------------------------------------------------- 模型缺陷与风控规则
#
# ⚠ 这张表是**给读代码的人看的免责声明**，同时也是页面「模型边界与风控规则」的数据源。
#    新增分项或改锚点前先扫一遍：这四条是模型的已知失效场景，不是 bug，
#    但读分时必须知道——否则会把「流动性事件」误读成「政策转向」，把「季末缴税」误读成「信用扩张」。
#
#    字段约定：
#      id      —— 稳定键，前端/文档引用用，别改名
#      tag     —— 出现在页面上的短标签（≤6 字）
#      title   —— 缺陷一句话概括
#      trigger —— 什么情况下会触发（可观测的信号，不是猜测）
#      action  —— 触发后该怎么处理（降置信度 / 打标记 / 人工复核 / 不作为依据）
#      note    —— 机制解释：为什么会这样，以及误读会导致什么结论错误
#      severe  —— true = 会直接让分项读数失真（需要人工介入）；false = 只是噪声放大

MODEL_LIMITS: List[Dict[str, Any]] = [
    # ① 存单的「流动性属性」会盖过「负债成本属性」
    #    存单是银行主动负债工具，但同时也是理财/基金手里流动性最好的短久期资产之一。
    #    一旦出现理财大规模赎回、债市踩踏，抛压会先把存单收益率顶上去 —— 此时读数反映的是
    #    「谁在被迫卖」，而不是「银行融资变贵」。方向（收益率上行＝分数下降）不算错，
    #    但归因错了：不是政策收紧，是流动性负反馈。这种时候资金面温度只能当「紧张事实」用，
    #    不能当「紧的持续性」用，必须人工复核后再下结论。
    {
        "id": "ncd_liquidity_distortion",
        "tag": "存单失真",
        "title": "理财赎回 / 债市踩踏时，NCD 脱离基本面流动性逻辑",
        "trigger": (
            "存单单日变动 > 20bp，或存单与 DR007 同向大幅抬升而回购成交量骤降；"
            "或同期出现理财负收益、信用债取消发行、债基赎回等消息"
        ),
        "action": (
            "资金面分项置信度下调，① 资金面温度暂不作为加减仓依据；"
            "改用 DR007 偏离 + R001−DR001 两项交叉确认，必要时人工复核后再更新结论"
        ),
        "note": (
            "机制：赎回压力下存单是被优先抛售的资产，收益率上行反映流动性挤兑，不是银行主动负债成本抬升；"
            "误读后果：会把「流动性事件」当成「货币转向」，在踩踏底部给出最紧的资金面读数"
        ),
        "severe": True,
    },
    # ② 财政存款的月度季节性极强
    #    财政存款是「国库里的钱」，其月度变化主要不是经济行为，而是预算节奏：
    #    12 月集中支出 → 财政存款大幅减少（按我们的反向锚点＝高分）；
    #    1 月缴税、地方债发行缴款 → 财政存款大幅增加（＝低分）。
    #    3/6/9 季末也有明显脉冲（财政支出冲刺 + 缴税）。这些都不是政策转向。
    {
        "id": "fiscal_deposit_seasonality",
        "tag": "季末脉冲",
        "title": "财政存款月度季节性极强，季末 / 年末读数会被预算节奏主导",
        "trigger": "数据月份为 12 月（年末）或 3 / 6 / 9 月（季末）",
        "action": (
            "卡片打「季末脉冲 / 年末脉冲」季节标记（见 SEASON_TAGS），"
            "该分项由「趋势信号」降级为「参考读数」；判读时对照去年同期而非上月环比"
        ),
        "note": (
            "机制：12 月财政集中支出、1 月缴税与地方债缴款，都是预算日历而非景气变化；"
            "误读后果：把年末支出当成「财政大放水」，把 1 月缴税当成「财政转向紧缩」"
        ),
        "severe": False,
    },
    # ③ 春节错月会把信贷单月同比多增打成噪声
    #    春节落在 1 月还是 2 月，会让当月工作日数差出 7~10 天，银行信贷投放整体平移。
    #    单月同比多增因此可能从 +4000 亿翻到 −4000 亿，纯日历效应。
    {
        "id": "spring_festival_distortion",
        "tag": "春节错月",
        "title": "信贷同比多增在 1 / 2 月波动极端大（春节错月）",
        "trigger": "数据月份为 1 月或 2 月（春节窗口，具体落在哪个月逐年不同）",
        "action": (
            "卡片打「春节月」标记，③ 两项（企业中长贷 / 住户中长贷）同比多增仅作参考；"
            "需要 1-2 月合并（累计口径）再看，或等 3 月读数确认趋势"
        ),
        "note": (
            "机制：春节错月导致工作日数差 7~10 天，投放节奏整体前后平移；"
            "误读后果：把日历效应当成「信贷需求崩了」或「开门红超预期」"
        ),
        "severe": False,
    },
    # ④ 模型定位：景气扩张打分器，不是金融风险模型
    #    这套分数的落点是「经济在扩张还是收缩、钱松还是紧」，不回答「哪里会爆」。
    #    居民杠杆率、地产链（销售/投资/房企现金流）、地方债务、中小金融机构风险，
    #    都是**外挂标签**——需要单独看，不体现在 0-100 分里。
    {
        "id": "not_a_risk_model",
        "tag": "定位边界",
        "title": "整套模型是景气扩张打分，不是金融风险模型",
        "trigger": "任何读数（这条永久生效，不是条件触发）",
        "action": (
            "居民杠杆率、地产风险、地方债务等尾部风险必须作为外挂标签单独跟踪；"
            "分数「中性」不等于「风险中性」，不能据此认为没有系统性风险"
        ),
        "note": (
            "机制：四项都能被「政策强刺激下的景气回升」点亮，即便同期杠杆在恶化；"
            "误读后果：在杠杆失控、地产失速的环境里，因为分数偏热而误判为「可以承受更多风险」"
        ),
        "severe": True,
    },
]


# 季节标记：给卡片挂一个「这个月的读数别当趋势读」的提示（不参与打分，纯提示）。
# 与 MODEL_LIMITS 里的 ②③ 两条对应 —— 缺陷写在这里，标记打在那里，两处必须同时维护。
SEASON_TAGS: Dict[str, Dict[str, Any]] = {
    # 财政存款：季末 + 年末，预算节奏主导
    "fiscal_deposit_mom": {
        "months": {
            3: "季末脉冲",
            6: "季末脉冲",
            9: "季末脉冲",
            12: "年末脉冲",
        },
        "note": "季末 / 年末财政集中收付与缴税叠加，当月环比波动放大，对照去年同期看、别按月环比读趋势",
    },
    # 信贷两项：1 / 2 月是春节窗口（春节落在哪个月逐年不同，两个都标）
    "corp_ml_loan_yoy": {
        "months": {1: "春节月", 2: "春节月"},
        "note": "春节错月会让工作日数差 7~10 天，单月同比多增仅作参考，看 1-2 月合并口径",
    },
    "household_ml_loan_yoy": {
        "months": {1: "春节月", 2: "春节月"},
        "note": "春节错月会让工作日数差 7~10 天，单月同比多增仅作参考，看 1-2 月合并口径",
    },
}


def season_tag(key: str, as_of: Any) -> Optional[Dict[str, Any]]:
    """给某指标的某期数据打季节标记；不需要标记时返回 None。

    as_of 形如 "2026-09" / "2026-09-30" / "兜底默认值"，只解析出月份，
    解析不出来就当没有标记（宁可不标，也不误标）。
    """
    cfg = SEASON_TAGS.get(key)
    if not cfg or not as_of:
        return None
    text = str(as_of)
    if len(text) < 7 or not text[:4].isdigit():
        return None
    try:
        month = int(text[5:7])
    except ValueError:
        return None
    label = cfg["months"].get(month)
    if not label:
        return None
    return {"label": label, "month": month, "note": cfg["note"]}


# ---------------------------------------------------------------- 打分

def zone_of(score: Optional[float]) -> Optional[str]:
    """把 0-100 分数映射到五段冷热色位（ice/cold/neutral/warm/hot）。"""
    if score is None:
        return None
    for upper, zone in ZONE_BANDS:
        if score < upper:
            return zone
    return "hot"


def state_of(score: Optional[float]) -> Optional[str]:
    """把 0-100 分数映射到中文状态标签（供分项表的「状态」列展示）。"""
    zone = zone_of(score)
    return ZONE_LABELS.get(zone) if zone else None


def lerp_score(value: Optional[float], anchors: Sequence[Tuple[float, float]]) -> Optional[float]:
    """两点间线性插值打分；超出锚点范围取端点分值（不外推）。"""
    if value is None or not anchors:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
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


def _collect_metrics(items: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """收集各分项的读数与**数据日期**。

    返回 (metrics, dates)：metrics 是 {key: value}，dates 是 {key: as_of}。
    日期随读数一起透传到 components，前端才能在表格里显示「这条读数是哪天的」——
    宏观指标频率混杂（日频 DR007 / 存单、月度社融 / PMI），不给日期容易把
    不同期的数据误当成同一时点比较。
    """
    metrics: Dict[str, Any] = {}
    dates: Dict[str, str] = {}
    for spec in SUB_INDICES:
        for comp in spec["components"]:
            item = items.get(comp["key"]) or {}
            metrics[comp["key"]] = item.get("value")
            if item.get("as_of"):
                dates[comp["key"]] = str(item["as_of"])
    return metrics, dates


def _credit_detail(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """③ 三项的取数明细（当月/去年同月/同比多增、辅助观测项），供页面「取数与口径」表。

    抓取时把整份明细塞进了 detail["_全量口径"]，这里取最新拿到的那一份即可
    （三项来自同一份《金融统计数据报告》，明细内容相同）。
    """
    for key in CREDIT_ITEM_KEYS:
        detail = (items.get(key) or {}).get("detail") or {}
        full = detail.get("_全量口径")
        if isinstance(full, dict) and full:
            return full
    return {}


def _sub_index(spec: Dict[str, Any], metrics: Dict[str, Any],
               dates: Dict[str, str]) -> Dict[str, Any]:
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
            "as_of": dates.get(comp["key"]),
            "score": None if score is None else round(score, 1),
            "zone": zone_of(score),
            "state": state_of(score),
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
        "state": state_of(score),
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
        return {"label": "数据不足", "hint": "资金面或信用部门温度缺失", "tone": "neutral"}
    for band in SPREAD_BANDS:
        if spread < band["under"]:
            return dict(band)
    return dict(SPREAD_BANDS[-1])


def compute_thermometer(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """合成宏观温度计。items 为 build_items() 的产出（含 value / detail）。"""
    metrics, dates = _collect_metrics(items)
    subs = [_sub_index(spec, metrics, dates) for spec in SUB_INDICES]

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
        # 总分与分项共用同一套五段阈值（0-20 低迷 / 20-40 偏冷 / 40-60 中性 / 60-80 偏热 / 80-100 亢奋）
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
        "zone_labels": dict(ZONE_LABELS),
        "zone_bands": [{"under": u, "zone": z, "label": ZONE_LABELS.get(z)}
                       for u, z in ZONE_BANDS],
        "derived_detail": _credit_detail(items),
        "missing": [s["name"] for s in subs if s["score"] is None],
        "usage_steps": list(USAGE_STEPS),
        "reading_note": READING_NOTE,
        "spread_note": SPREAD_NOTE,
        "caveat_note": CAVEAT_NOTE,
        # 模型缺陷与风控规则（见文件顶部 MODEL_LIMITS 的注释）；页面「模型边界与风控规则」直接渲染这张表
        "model_limits": [dict(x) for x in MODEL_LIMITS],
    }
