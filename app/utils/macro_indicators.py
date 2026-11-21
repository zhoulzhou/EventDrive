"""宏观盯盘清单（三个维度）数据获取、状态判定与组合解读。

三个维度、共 10 项读数：

- ① 资金面（松 ↔ 紧，日度）：DR001 − 政策利率、1Y AAA 同业存单收益率、R001 − DR001
- ② 经济热度（冷 ↔ 热，月度）：制造业 PMI、PPI 同比、社融存量同比、M1 − M2 剪刀差
- ③ 信用 / 部门（资金活化 ↔ 空转，月度）：企业中长贷同比多增、住户中长贷同比多增、财政存款当月增量

A股估值/情绪类读数（沪深300 PE/PB、ERP、成交额、两融）**不在本页**，
见「股票指标」页（app/utils/stock_temp.py）。

数据获取方式（全部自动，来源见各自 source 字段）：
- akshare（source="akshare"）
          · interest_rate_repo_hist_cm  → FDR001 / FR001（即 DR001 / R001 定盘价）
          · bond_zh_close_yield_cm      → 同业存单(AAA) 收盘收益率曲线 1Y
          · macro_china_pmi             → 制造业 PMI
          · macro_china_ppi             → PPI 当月同比
          · macro_china_money_supply_em    → M1 / M2 同比
- 央行官网（source="pbc"，见 app/utils/pbc_data.py）
          · 《公开市场业务交易公告》      → 7天逆回购操作利率（每个交易日）
          · 《金融统计数据报告》          → 社融存量同比、企业与住户中长贷、财政存款（每月）

全部指标均为自动获取，页面不提供任何手工录入入口。

任一项抓取失败时不做异常抛出，对应指标返回 value=None 并带 error，页面按
"数据缺失"展示并保留上一次成功读数，不影响其余指标与页面渲染。
"""
import json
import logging
from datetime import date, timedelta
from statistics import mean
from typing import Any, Dict, List, Optional

from app.utils import macro_thermometer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 分组与色调
#
# 页面按「三个维度」分组，顺序即页面上从上到下的顺序：
# 钱贵不贵（资金面）→ 经济跑不跑（热度）→ 钱在谁手里（部门结构）。

GROUP_LIQUIDITY = "liquidity"
GROUP_HEAT = "heat"
GROUP_STRUCTURE = "structure"

GROUP_ORDER = [GROUP_LIQUIDITY, GROUP_HEAT, GROUP_STRUCTURE]

GROUP_TITLES = {
    GROUP_LIQUIDITY: "① 资金面（松 ↔ 紧）",
    GROUP_HEAT: "② 经济热度（冷 ↔ 热）",
    GROUP_STRUCTURE: "③ 部门结构（钱的流向）",
}

# 各维度的观察频率（放在维度标题右侧，提醒「日度盯异动、月度盯趋势」）
GROUP_FREQS = {
    GROUP_LIQUIDITY: "日度",
    GROUP_HEAT: "月度",
    GROUP_STRUCTURE: "月度 · 累计口径",
}

GROUP_DESCS = {
    GROUP_LIQUIDITY: "钱贵不贵：看「价」（DR001 偏离、1Y 同业存单）与「分层」（R001−DR001）",
    GROUP_HEAT: "经济跑不跑：看「量」（社融）与「温度」（PMI、PPI、M1−M2）",
    GROUP_STRUCTURE: "钱在谁手里：看企业中长贷、住户中长贷与财政存款，判断信用扩张与资金回笼",
}

# 色调 -> 前端配色（前端按 tone 取色）
TONES = {
    "blue": "#3b82f6",
    "cyan": "#06b6d4",
    "green": "#22c55e",
    "lime": "#84cc16",
    "amber": "#f59e0b",
    "orange": "#f97316",
    "red": "#ef4444",
}

# ---------------------------------------------------------------- 指标清单

INDICATORS: List[Dict[str, Any]] = [
    {
        "key": "dr001_spread",
        "group": GROUP_LIQUIDITY,
        "name": "DR001 − 政策利率（隔夜）",
        "subtitle": "隔夜资金价格 vs 央行政策利率锚",
        "unit": "bp",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "DR001 是存款类机构（银行）隔夜期质押式回购加权利率；逆回购操作利率是央行"
            "公开市场操作的锚（随公告更新，当前为隔夜品种 1.25%）。两者之差即银行隔夜"
            "拿钱的成本相对政策利率偏离了多少，正值表示银行体系隔夜资金偏紧。"
        ),
        "guide": (
            "看「价」。负偏离 = 隔夜资金比政策利率还便宜，说明钱多；正偏离持续扩大 = "
            "银行体系缺钱，缴税、政府债缴款高峰时常出现；+35bp 以上属明显紧张。"
        ),
        "bands": [
            {"range": "≤ −5bp", "state": "很松，钱富余", "tone": "blue"},
            {"range": "−5 ~ 0bp", "state": "偏松", "tone": "cyan"},
            {"range": "0 ~ +10bp", "state": "平稳", "tone": "green"},
            {"range": "+10 ~ +20bp", "state": "略偏紧", "tone": "amber"},
            {"range": "+20 ~ +35bp", "state": "偏紧", "tone": "orange"},
            {"range": "≥ +55bp", "state": "钱荒级别", "tone": "red"},
        ],
    },
    {
        "key": "ncd_1y_aaa",
        "group": GROUP_LIQUIDITY,
        "name": "1Y AAA 同业存单",
        "subtitle": "银行负债成本（中国货币网收盘收益率曲线）",
        "unit": "%",
        "digits": 3,
        "source": "auto",
        "meaning": (
            "1 年期 AAA 同业存单到期收益率，来自中国货币网「收盘收益率曲线 → 同业存单(AAA)」"
            "（中债估值口径）。它是银行主动负债的边际成本，也是资金面「水位」的价格化体现。"
        ),
        "guide": (
            "看「水位」。收益率低位 = 银行负债压力小、资金宽裕；快速上行 = 负债端收紧，"
            "往往先于信贷投放放缓出现。"
        ),
        "bands": [
            {"range": "≤ 1.00%", "state": "极低，资金宽松", "tone": "blue"},
            {"range": "1.00 ~ 1.30%", "state": "低位", "tone": "cyan"},
            {"range": "1.30 ~ 1.60%", "state": "中性", "tone": "green"},
            {"range": "1.60 ~ 1.95%", "state": "偏高，负债成本抬升", "tone": "amber"},
            {"range": "≥ 2.30%", "state": "高位，融资成本高", "tone": "red"},
        ],
    },
    {
        "key": "r001_dr001",
        "group": GROUP_LIQUIDITY,
        "name": "R001 − DR001",
        "subtitle": "非银分层",
        "unit": "bp",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "R001 是银行间全市场（含非银机构）隔夜质押式回购加权利率，DR001 是存款类机构"
            "（银行）隔夜回购加权利率。二者之差衡量\"非银分层\"——非银机构借钱比银行贵多少。"
        ),
        "guide": (
            "看分层。利差小 = 银行与非银融资成本接近，流动性传导顺畅；利差走阔 = 非银"
            "（基金、券商、理财）借钱变贵，常见于资金收紧、去杠杆或年末考核时点；"
            ">50bp 属极端（2020 年 12 月曾冲上百 bp）。"
        ),
        "bands": [
            {"range": "< 15bp", "state": "正常", "note": "流动性传导顺畅", "tone": "green"},
            {"range": "15 ~ 30bp", "state": "分层走阔", "note": "非银融资成本抬升", "tone": "amber"},
            {"range": "30 ~ 50bp", "state": "结构性紧张，非银借钱变贵", "note": "非银明显更贵", "tone": "orange"},
            {"range": "> 50bp", "state": "极端", "note": "2020 年 12 月曾冲上百 bp", "tone": "red"},
        ],
    },
    {
        "key": "pmi",
        "group": GROUP_HEAT,
        "name": "制造业 PMI",
        "subtitle": "经济景气领先指标",
        "unit": "",
        "digits": 1,
        "source": "auto",
        "meaning": (
            "制造业采购经理指数，国家统计局按月发布（次月初公布），是经济景气的领先指标。"
            "50 为荣枯线：>50 扩张、<50 收缩。"
        ),
        "guide": (
            "看\"温度\"中公布最早的月度数据。48~50 = 荣枯线附近磨底 / 弱复苏；"
            "连续高于 50 且上行 = 复苏确认；跌破 48 需警惕。"
        ),
        "bands": [
            {"range": "≥ 52", "state": "强劲扩张", "tone": "red"},
            {"range": "50 ~ 52", "state": "温和扩张", "tone": "amber"},
            {"range": "48 ~ 50", "state": "荣枯线附近磨底", "note": "弱复苏", "tone": "green"},
            {"range": "45 ~ 48", "state": "明显收缩", "tone": "cyan"},
            {"range": "< 45", "state": "深度收缩", "tone": "blue"},
        ],
    },
    {
        "key": "tsf_yoy",
        "group": GROUP_HEAT,
        "name": "社融存量同比",
        "subtitle": "宽信用 / 信用收缩",
        "unit": "%",
        "digits": 1,
        "source": "auto",
        "meaning": (
            "社会融资规模存量同比，衡量实体经济从金融体系获得的资金总量增速，是\"宽信用\""
            "最直接的度量。由央行按月发布（akshare 只有社融\"增量\"口径，故走央行官网）。"
        ),
        "guide": (
            "看\"量\"。社融存量同比上行 = 信用扩张、实体融资需求改善；下行 = 信用收缩，"
            "\"宽货币\"没进实体。是判断\"资金松 + 信用紧\"组合的关键。"
        ),
        "bands": [
            {"range": "> 12%", "state": "宽信用 / 过热（2020 年曾 13.3%，随后政策转向）", "tone": "red"},
            {"range": "10 ~ 12%", "state": "偏宽", "tone": "amber"},
            {"range": "8 ~ 10%", "state": "中性（2018–2021 常态）", "tone": "green"},
            {"range": "< 8%", "state": "信用收缩", "tone": "blue"},
        ],
    },
    {
        "key": "m1_m2",
        "group": GROUP_STRUCTURE,
        "name": "M1 − M2 剪刀差",
        "subtitle": "资金活化程度",
        "unit": "pp",
        "digits": 1,
        "source": "auto",
        "meaning": (
            "M1 同比 减去 M2 同比。M1（狭义货币）以企业活期存款和现金为主，代表\"活钱\"、"
            "交易性需求；M2（广义货币）含定期存款，代表\"沉淀的钱\"。剪刀差为正说明资金在活化、"
            "企业和居民更愿意交易投资。"
        ),
        "guide": (
            "剪刀差回升（负值收窄）= 资金活化、经济热度上行，通常领先于企业与权益市场"
            "预期改善；持续为负且扩大 = 资金淤积、需求偏弱。"
            "本维度要和分部门存贷款一起读：钱的\"总量\"没变但换了口袋，也算结构变化。"
        ),
        "bands": [
            {"range": "> 0", "state": "资金活化强、经济热度高", "note": "2016–17 牛市区间", "tone": "red"},
            {"range": "0 ~ −3%", "state": "中性", "tone": "green"},
            {"range": "−3 ~ −6%", "state": "偏冷", "tone": "cyan"},
            {"range": "< −6%", "state": "深度偏冷", "note": "2024 年曾到约 −8%", "tone": "blue"},
        ],
    },
    {
        "key": "ppi_yoy",
        "group": GROUP_HEAT,
        "name": "PPI 同比",
        "subtitle": "工业品价格与上游成本压力",
        "unit": "%",
        "digits": 1,
        "source": "auto",
        "meaning": (
            "工业生产者出厂价格指数同比，衡量工业品出厂价格变化，反映工业供需状况与上游"
            "成本压力。国家统计局按月发布。"
        ),
        "guide": (
            "看工业品的\"体温\"。PPI 转正且温和上行 = 供需改善、企业盈利修复；>5% 需警惕"
            "成本向中下游挤压；持续深度为负 = 工业通缩。"
        ),
        "bands": [
            {"range": "> 5%", "state": "过热，成本挤压中下游", "tone": "red"},
            {"range": "2 ~ 5%", "state": "偏热、供需两旺", "tone": "amber"},
            {"range": "0 ~ 2%", "state": "温和回升", "tone": "lime"},
            {"range": "−2 ~ 0%", "state": "通缩阴影", "tone": "cyan"},
            {"range": "−3 ~ −2%", "state": "偏冷", "tone": "cyan"},
            {"range": "< −3%", "state": "深度通缩（2023–2024 常态）", "tone": "blue"},
        ],
    },

    # ------------------------------------------------------------ ③ 信用 / 部门
    {
        "key": "corp_ml_yoy",
        "group": GROUP_STRUCTURE,
        "name": "企业中长贷 同比多增",
        "subtitle": "企业资本开支意愿（当月同比）",
        "unit": "亿",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "企（事）业单位中长期贷款当月增量相对去年同月的多增额。央行《金融统计数据报告》"
            "只披露年初至今累计数，本页先用相邻两期累计相减得到当月增量，再与去年同月比较。"
            "中长贷对应企业的资本开支与设备投资，是信用扩张「实不实」的核心观察。"
        ),
        "guide": (
            "同比多增 = 企业愿意借钱做长期投资；持续少增 = 企业收缩资本开支。"
            "短贷与票据冲量不算资本开支，本页不纳入打分。"
        ),
        "bands": [
            {"range": "多增 ≥ 4000 亿", "state": "企业扩张明显", "tone": "red"},
            {"range": "多增 2000 ~ 4000 亿", "state": "扩张意愿回升", "tone": "amber"},
            {"range": "0 ~ 2000 亿", "state": "小幅多增", "tone": "green"},
            {"range": "少增 0 ~ 2000 亿", "state": "边际收缩", "tone": "cyan"},
            {"range": "少增 ≥ 2000 亿", "state": "明显收缩", "tone": "blue"},
        ],
    },
    {
        "key": "household_ml_yoy",
        "group": GROUP_STRUCTURE,
        "name": "住户中长贷 同比多增",
        "subtitle": "居民按揭与长期消费（当月同比）",
        "unit": "亿",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "住户中长期贷款当月增量相对去年同月的多增额，口径同上（累计相减 + 同比）。"
            "住户中长贷以按揭为主，直接反映居民加杠杆与地产销售意愿。"
        ),
        "guide": (
            "同比多增 = 居民重新加杠杆，通常对应地产销售与消费信心修复；"
            "持续少增 = 居民提前还贷、缩表。"
        ),
        "bands": [
            {"range": "多增 ≥ 3000 亿", "state": "居民扩张明显", "tone": "red"},
            {"range": "多增 1500 ~ 3000 亿", "state": "加杠杆意愿回升", "tone": "amber"},
            {"range": "0 ~ 1500 亿", "state": "小幅多增", "tone": "green"},
            {"range": "少增 0 ~ 1500 亿", "state": "边际收缩", "tone": "cyan"},
            {"range": "少增 ≥ 1500 亿", "state": "居民缩表", "tone": "blue"},
        ],
    },
    {
        "key": "fiscal_deposit_mom",
        "group": GROUP_STRUCTURE,
        "name": "财政存款 当月增量",
        "subtitle": "财政投放还是回笼",
        "unit": "亿",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "财政性存款当月增量（相邻两期累计相减）。财政存款增加 = 税款/债券资金进入国库、"
            "从银行体系抽走流动性；减少 = 财政支出投放流动性。"
        ),
        "guide": (
            "减少（尤其 ≥3000 亿）= 财政投放，利好流动性；增加（尤其 ≥3000 亿）= 财政回笼，"
            "抽走银行体系资金，常出现在缴税大月与政府债发行高峰。"
        ),
        "bands": [
            {"range": "减少 ≥ 3000 亿", "state": "财政大幅投放", "tone": "green"},
            {"range": "减少 0 ~ 3000 亿", "state": "财政小幅投放", "tone": "lime"},
            {"range": "增加 0 ~ 3000 亿", "state": "财政小幅回笼", "tone": "amber"},
            {"range": "增加 ≥ 3000 亿", "state": "财政明显回笼", "tone": "red"},
        ],
    },
]

INDICATOR_MAP = {ind["key"]: ind for ind in INDICATORS}
INDICATOR_KEYS = [ind["key"] for ind in INDICATORS]

# 仅用于图表的历史序列键：不作为指标卡片展示，只存进历史表供趋势图取数。
# 例如「M1/M2 双线 + 剪刀差填充」需要 M1 同比、M2 同比两条独立序列。
SERIES_EXTRA_KEYS = {
    "m1_yoy": "M1 同比",
    "m2_yoy": "M2 同比",
}

# ------------------------------------------------------------ 参数与抓取键

# 逆回购操作利率：央行政策利率锚，用于计算 DR001 偏离
# （期限随央行公告变化：近年以隔夜为主，历史上为 7 天期；自动抓取自《公开市场业务交易公告》）
PARAM_REVERSE_REPO = "reverse_repo_rate"
PARAM_META = {
    PARAM_REVERSE_REPO: {
        "name": "逆回购操作利率（政策利率锚）",
        "unit": "%",
        "digits": 2,
        "default": 1.25,
        "hint": "央行最新一期公开市场逆回购操作利率（随《公开市场业务交易公告》更新，期限可能为隔夜或 7 天期），用于计算 DR001 偏离。",
    },
}

# 抓取失败且库中无记录时的兜底读数（保证页面有数可展示，source 标记为 fallback）
FALLBACK_DEFAULTS: Dict[str, Any] = {
    "tsf_yoy": {"value": 7.2, "as_of": "兜底默认值"},
    PARAM_REVERSE_REPO: {"value": 1.25, "as_of": "兜底默认值"},
}

# 自动抓取键：akshare 来源 + 央行官网来源（无手工入口，全部自动）
AK_KEYS = ["dr001_spread", "ncd_1y_aaa", "r001_dr001", "pmi", "m1_m2", "ppi_yoy"]
PBC_KEYS = ["tsf_yoy", "corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom",
            PARAM_REVERSE_REPO]
AUTO_KEYS = AK_KEYS + PBC_KEYS

# 有效自动来源；库中记录的 source 不在此集合内（如历史遗留的 manual）即视为待刷新
AUTO_SOURCES = {"akshare", "pbc"}

# 数据来源展示标签
SOURCE_LABELS = {
    "akshare": "akshare",
    "pbc": "央行",
    "fallback": "默认值",
}

# 历史接口允许查询的全部键（指标 + 参数锚 + 图表专用序列）
HISTORY_KEYS = set(INDICATOR_KEYS) | {PARAM_REVERSE_REPO} | set(SERIES_EXTRA_KEYS)

# 各键的中文名（含政策利率锚与图表专用序列）
NAME_OF_KEY = {ind["key"]: ind["name"] for ind in INDICATORS}
NAME_OF_KEY[PARAM_REVERSE_REPO] = PARAM_META[PARAM_REVERSE_REPO]["name"]
NAME_OF_KEY.update(SERIES_EXTRA_KEYS)

# 图表面板「三组一定位」用到的序列键（供 /api/macro/series 组装）
CHART_DAILY_KEYS = ["dr001_spread", "r001_dr001"]
CHART_MONTHLY_KEYS = ["pmi", "ppi_yoy", "tsf_yoy", "m1_yoy", "m2_yoy", "m1_m2",
                      "corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"]
CHART_KEYS = CHART_DAILY_KEYS + CHART_MONTHLY_KEYS

# ---------------------------------------------------------------- 状态判定


def _decide(key: str, value: Optional[float]):
    """按指标阈值把读数判定为 (状态标签, 色调, 严重度等级)。

    资金面：等级 0=最松 → 5=最紧；②③：等级 0=最冷/最淤积 → 4=最热/最活化。
    """
    if value is None:
        return None
    v = float(value)

    if key == "dr001_spread":  # bp
        if v <= -5:
            return ("很松，钱富余", "blue", 0)
        if v < 0:
            return ("偏松", "cyan", 1)
        if v < 10:
            return ("平稳", "green", 2)
        if v < 20:
            return ("略偏紧", "amber", 3)
        if v < 35:
            return ("偏紧", "orange", 4)
        if v < 55:
            return ("明显紧张", "orange", 4)
        return ("钱荒级别", "red", 5)

    if key == "ncd_1y_aaa":  # %
        if v <= 1.0:
            return ("极低，资金宽松", "blue", 0)
        if v < 1.3:
            return ("低位，宽松", "cyan", 1)
        if v < 1.6:
            return ("中性", "green", 2)
        if v < 1.95:
            return ("偏高，负债成本抬升", "amber", 3)
        return ("高位，融资成本高", "red", 4)

    if key == "r001_dr001":  # bp
        if v < 15:
            return ("正常", "green", 2)
        if v < 30:
            return ("分层走阔", "amber", 3)
        if v < 50:
            return ("结构性紧张，非银借钱变贵", "orange", 4)
        return ("极端", "red", 5)

    if key == "pmi":
        if v >= 52:
            return ("强劲扩张", "red", 4)
        if v >= 50:
            return ("温和扩张", "amber", 3)
        if v >= 48:
            return ("荣枯线附近磨底", "green", 2)
        if v >= 45:
            return ("明显收缩", "cyan", 1)
        return ("深度收缩", "blue", 0)

    if key == "tsf_yoy":  # %
        if v > 12:
            return ("宽信用 / 过热", "red", 4)
        if v >= 10:
            return ("偏宽", "amber", 3)
        if v >= 8:
            return ("中性", "green", 2)
        return ("信用收缩", "blue", 0)

    if key == "m1_m2":  # pp
        if v > 0:
            return ("资金活化强、经济热度高", "red", 4)
        if v >= -3:
            return ("中性", "green", 2)
        if v >= -6:
            return ("偏冷", "cyan", 1)
        return ("深度偏冷", "blue", 0)

    if key == "ppi_yoy":  # %
        if v > 5:
            return ("过热，成本挤压中下游", "red", 4)
        if v >= 2:
            return ("偏热、供需两旺", "amber", 3)
        if v >= 0:
            return ("温和回升", "lime", 2)
        if v >= -2:
            return ("通缩阴影", "cyan", 1)
        if v >= -3:
            return ("偏冷", "cyan", 1)
        return ("深度通缩", "blue", 0)

    # ---------------- ③ 信用 / 部门（0=收缩/回笼 → 4=扩张/投放）
    if key == "corp_ml_yoy":  # 亿元（同比多增）
        if v >= 4000:
            return ("企业扩张明显", "red", 4)
        if v >= 2000:
            return ("扩张意愿回升", "amber", 3)
        if v >= 0:
            return ("小幅多增", "green", 2)
        if v >= -2000:
            return ("边际收缩", "cyan", 1)
        return ("明显收缩", "blue", 0)

    if key == "household_ml_yoy":  # 亿元（同比多增）
        if v >= 3000:
            return ("居民扩张明显", "red", 4)
        if v >= 1500:
            return ("加杠杆意愿回升", "amber", 3)
        if v >= 0:
            return ("小幅多增", "green", 2)
        if v >= -1500:
            return ("边际收缩", "cyan", 1)
        return ("居民缩表", "blue", 0)

    if key == "fiscal_deposit_mom":  # 亿元（当月增量）
        if v <= -3000:
            return ("财政大幅投放", "green", 4)
        if v < 0:
            return ("财政小幅投放", "lime", 3)
        if v < 3000:
            return ("财政小幅回笼", "amber", 1)
        return ("财政明显回笼", "red", 0)

    return None


# ---------------------------------------------------------------- akshare 抓取


def _import_akshare():
    """延迟导入 akshare；未安装时返回 None，由调用方按数据缺失处理。"""
    try:
        import akshare as ak  # noqa: F401
        return ak
    except Exception as exc:  # pragma: no cover - 环境相关
        logger.warning("akshare 不可用，市场指标将无法自动更新: %s", exc)
        return None


def _cn_month_to_iso(raw: Any) -> str:
    """把 "2026年08月份" / "2026-08" / "202608" 统一成 "2026-08"。"""
    text = str(raw).strip()
    if "年" in text:
        year = text.split("年")[0]
        rest = text.split("年")[1]
        month = rest.replace("月份", "").replace("月", "").strip()
        if month.isdigit():
            return "%s-%02d" % (year, int(month))
        return text
    if "-" in text:
        return text[:7]
    if len(text) == 6 and text.isdigit():
        return "%s-%s" % (text[:4], text[4:6])
    return text


def _latest_row(df, sort_col: str):
    """在列名兼容的 DataFrame 中取"最近一期"记录（按 sort_col 排序取最后一行）。"""
    if df is None or df.empty or sort_col not in df.columns:
        return None
    tmp = df.dropna(subset=[sort_col]).copy()
    if tmp.empty:
        return None
    tmp["_k"] = tmp[sort_col].map(_cn_month_to_iso)
    tmp = tmp.sort_values("_k")
    return tmp.iloc[-1]


def _fetch_repo_rates(ak) -> Dict[str, Any]:
    """抓取最近的银行间回购定盘利率（FDR001/FDR007 即 DR001/DR007，FR001 即 R001）。

    repo_rate_hist 要求起止日期在一个月内，故取本月 1 日至今天；若本月尚无数据
    （月初），回退到上一个月区间重试。
    """
    today = date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    windows = [
        (first_of_month, today),
        (last_month_end.replace(day=1), last_month_end),
    ]
    last_error = None
    for start, end in windows:
        try:
            fn = getattr(ak, "interest_rate_repo_hist_cm", None) or ak.repo_rate_hist
            df = fn(
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
            )
        except Exception as exc:
            last_error = exc
            continue
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["date"])
        if df.empty:
            continue
        row = df.iloc[-1]
        return {
            "date": str(row["date"])[:10],
            "FR001": None if row.get("FR001") is None else float(row["FR001"]),
            "FDR001": None if row.get("FDR001") is None else float(row["FDR001"]),
            "FDR007": None if row.get("FDR007") is None else float(row["FDR007"]),
        }
    raise RuntimeError("回购定盘利率抓取失败: %s" % (last_error or "区间内无数据"))


def _fetch_ncd_1y(ak) -> Dict[str, Any]:
    """抓取中国货币网「同业存单(AAA)」收盘收益率曲线的 1Y 到期收益率。"""
    today = date.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    windows = [
        (first_of_month, today),
        (last_month_end.replace(day=1), last_month_end),
    ]
    last_error = None
    for start, end in windows:
        try:
            df = ak.bond_zh_close_yield_cm(
                symbol="同业存单(AAA)", period="1",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
            )
        except Exception as exc:
            last_error = exc
            continue
        if df is None or df.empty or "期限" not in df.columns:
            continue
        one_year = df[df["期限"].astype(float) == 1.0].copy()
        if one_year.empty:
            continue
        one_year["_d"] = one_year["日期"].astype(str)
        latest_date = one_year["_d"].max()
        row = one_year[one_year["_d"] == latest_date].iloc[-1]
        return {
            "value": round(float(row["到期收益率"]), 3),
            "as_of": latest_date[:10],
            "detail": {
                "曲线": "同业存单(AAA)",
                "期限": "1Y",
                "即期收益率": None if row.get("即期收益率") is None else round(float(row["即期收益率"]), 3),
            },
        }
    raise RuntimeError("同业存单 1Y 收益率抓取失败: %s" % (last_error or "区间内无 1Y 数据"))


def _fetch_pmi(ak) -> Dict[str, Any]:
    df = ak.macro_china_pmi()
    row = _latest_row(df, "月份")
    if row is None:
        raise RuntimeError("制造业 PMI 无有效数据")
    return {
        "value": float(row["制造业-指数"]),
        "as_of": _cn_month_to_iso(row["月份"]),
        "detail": {
            "非制造业": None if row.get("非制造业-指数") is None else float(row["非制造业-指数"])
        },
    }


def _fetch_ppi(ak) -> Dict[str, Any]:
    df = ak.macro_china_ppi()
    row = _latest_row(df, "月份")
    if row is None:
        raise RuntimeError("PPI 无有效数据")
    return {
        "value": float(row["当月同比增长"]),
        "as_of": _cn_month_to_iso(row["月份"]),
        "detail": {},
    }


def _fetch_money_supply(ak) -> Dict[str, Any]:
    fn = getattr(ak, "macro_china_money_supply_em", None) or ak.macro_china_money_supply
    df = fn()
    row = _latest_row(df, "月份")
    if row is None:
        raise RuntimeError("货币供应量无有效数据")
    m1 = float(row["货币(M1)-同比增长"])
    m2 = float(row["货币和准货币(M2)-同比增长"])
    return {
        "value": round(m1 - m2, 2),
        "as_of": _cn_month_to_iso(row["月份"]),
        "detail": {"M1同比": m1, "M2同比": m2},
    }


def _import_pbc():
    """延迟导入央行数据模块；不可用时返回 None。"""
    try:
        from app.utils import pbc_data
        return pbc_data
    except Exception as exc:  # pragma: no cover - 环境相关
        logger.warning("央行数据模块不可用: %s", exc)
        return None


def _item(value, as_of, source, detail=None) -> Dict[str, Any]:
    return {
        "value": value,
        "as_of": as_of,
        "source": source,
        "detail": detail or {},
        "error": None,
    }


def fetch_auto_values(
    anchor_fallback: Optional[float] = None,
    hints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """抓取全部自动指标，返回 {key: {value, as_of, source, detail, error}}。

    单个指标抓取失败只影响该指标（value=None + error），不影响其他指标。
    anchor_fallback：库中已有的 7 天逆回购利率，抓取失败时兜底。
    hints           ：各指标已落库的 detail，用于央行侧「报告期未变则跳过」省流量。
    """
    hints = hints or {}
    result: Dict[str, Dict[str, Any]] = {}

    def _fail(key: str, source: str, exc: Exception):
        logger.warning("市场指标抓取失败 [%s]: %s", key, exc)
        result[key] = {
            "value": None,
            "as_of": None,
            "source": source,
            "detail": {},
            "error": str(exc),
        }

    # ---------------- 央行官网：逆回购利率 / 社融 / 中长期贷款与财政存款
    pbc = _import_pbc()
    if pbc is None:
        for key in PBC_KEYS:
            _fail(key, "pbc", RuntimeError("央行数据模块不可用"))
    else:
        try:
            item = pbc.fetch_reverse_repo_rate()
            result[PARAM_REVERSE_REPO] = _item(
                item["value"], item.get("as_of"), "pbc", item.get("detail"))
        except Exception as exc:
            _fail(PARAM_REVERSE_REPO, "pbc", exc)

        try:
            item = pbc.fetch_tsf_stock_yoy()
            result["tsf_yoy"] = _item(
                item["value"], item.get("as_of"), "pbc", item.get("detail"))
        except Exception as exc:
            _fail("tsf_yoy", "pbc", exc)

        try:
            item = pbc.fetch_credit_breakdown()
            as_of = item.get("as_of")
            detail = item.get("detail") or {}
            for key in ("corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"):
                result[key] = _item(item.get(key), as_of, "pbc", detail)
        except Exception as exc:
            for key in ("corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"):
                _fail(key, "pbc", exc)

    # ---------------- akshare
    ak = _import_akshare()
    if ak is None:
        for key in AK_KEYS:
            _fail(key, "akshare", RuntimeError("当前运行环境未安装 akshare，无法自动获取"))
        return result

    # 政策利率锚：本次抓到的央行值 > 库中已有值 > 兜底默认值
    anchor = (result.get(PARAM_REVERSE_REPO) or {}).get("value")
    if anchor is None:
        anchor = anchor_fallback
    if anchor is None:
        anchor = PARAM_META[PARAM_REVERSE_REPO]["default"]

    repo = None
    try:
        repo = _fetch_repo_rates(ak)
    except Exception as exc:
        _fail("dr001_spread", "akshare", exc)
        _fail("r001_dr001", "akshare", exc)

    if repo is not None:
        fdr001 = repo.get("FDR001")
        fr001 = repo.get("FR001")

        if fdr001 is None:
            _fail("dr001_spread", "akshare", RuntimeError("缺少 FDR001 读数"))
        else:
            result["dr001_spread"] = _item(
                round((fdr001 - anchor) * 100, 1), repo["date"], "akshare",
                {"DR001": fdr001, "逆回购利率": anchor},
            )

        if fdr001 is None or fr001 is None:
            _fail("r001_dr001", "akshare", RuntimeError("缺少 FR001 / FDR001 读数"))
        else:
            result["r001_dr001"] = _item(
                round((fr001 - fdr001) * 100, 1), repo["date"], "akshare",
                {"R001": fr001, "DR001": fdr001},
            )

    for key, fetcher in (
        ("ncd_1y_aaa", _fetch_ncd_1y),
        ("pmi", _fetch_pmi),
        ("ppi_yoy", _fetch_ppi),
        ("m1_m2", _fetch_money_supply),
    ):
        try:
            item = fetcher(ak)
            result[key] = _item(item["value"], item["as_of"], "akshare", item.get("detail"))
        except Exception as exc:
            _fail(key, "akshare", exc)

    return result


# ---------------------------------------------------------------- 组合解读

QUADRANTS = [
    {
        "title": "资金松 + 信用紧",
        "brief": "宽货币没进实体",
        "desc": (
            "钱堆在银行间和债市，对应\"资产荒\"、长债利率低位。"
            "DR001 平 ≠ 经济好，必须拿社融、PMI 证伪。"
        ),
        "tone": "cyan",
    },
    {
        "title": "资金松 + 信用松",
        "brief": "全面宽松",
        "desc": "货币与信用同向扩张，股、债、商品往往同涨。",
        "tone": "green",
    },
    {
        "title": "资金紧 + 热度高",
        "brief": "政策收紧或通胀压制",
        "desc": "流动性收紧叠加经济偏热，存在股债双杀风险。",
        "tone": "orange",
    },
    {
        "title": "资金紧 + 热度低",
        "brief": "最差组合",
        "desc": "衰退 + 流动性收缩，风险资产承压最明显。",
        "tone": "red",
    },
]

MEMORY_LINE = (
    "钱贵不贵（资金面）→ 经济跑不跑（热度）→ 钱在谁手里（部门结构）。"
)

# 观察哨：从「底部/陷阱」走向全面反转时，最先要看到转向的三个读数
# 前两项在本页（② 社融、③ 住户贷款）可直接看到；第三项属于 A股 情绪面，
# 已随 ④A股温度 从本页移出，改在「股票指标」页看（成交额 turnover_amt / 两融 margin）。
WATCH_LIST = [
    {"key": "tsf_yoy", "label": "社融存量同比企稳（回到 8% 以上）"},
    {"key": "household_ml_yoy", "label": "住户中长贷同比转正（居民重新加杠杆）"},
    {"key": "turnover_amt", "label": "成交额与两融余额同向放大（情绪有杠杆支撑）"},
]


def _avg(values: List[int]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(mean(vals), 2) if vals else None


def compute_combination(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """由三个维度汇总出资金面松紧、经济热度与部门结构，并给出四象限归属。"""
    liq_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("dr001_spread", "ncd_1y_aaa", "r001_dr001")
    ])
    # 经济热度只放「量价」三项；M1−M2 归入③信用/部门（与温度计口径一致）
    heat_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("pmi", "tsf_yoy", "ppi_yoy")
    ])
    struct_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom")
    ])

    if liq_avg is None:
        liquidity = {"label": "数据不足", "tone": "green", "reason": "资金面指标缺失"}
        liq_side = None
    elif liq_avg < 2.5:
        liquidity = {"label": "资金松", "tone": "green", "reason": "资金价格与水位均不紧"}
        liq_side = "松"
    elif liq_avg < 3.5:
        liquidity = {"label": "资金中性", "tone": "amber", "reason": "资金面接近均衡、边际有扰动"}
        liq_side = "中性"
    else:
        liquidity = {"label": "资金紧", "tone": "red", "reason": "资金价格偏高或水位偏低"}
        liq_side = "紧"

    if heat_avg is None:
        heat = {"label": "数据不足", "tone": "green", "reason": "经济热度指标缺失"}
        heat_side = None
    elif heat_avg < 1.5:
        heat = {"label": "热度偏低", "tone": "cyan", "reason": "量价指标偏冷"}
        heat_side = "冷"
    elif heat_avg < 2.5:
        heat = {"label": "热度中性", "tone": "green", "reason": "冷热指标互有强弱"}
        heat_side = "中性"
    else:
        heat = {"label": "热度偏高", "tone": "red", "reason": "量价指标整体偏热"}
        heat_side = "热"

    if struct_avg is None:
        structure = {"label": "数据不足", "tone": "green", "reason": "部门结构指标缺失"}
    elif struct_avg < 1.5:
        structure = {"label": "钱淤积（居民缩表）", "tone": "blue", "reason": "剪刀差深负、居民净还贷"}
    elif struct_avg < 2.5:
        structure = {"label": "中性", "tone": "green", "reason": "活化与缩表信号互有强弱"}
    else:
        structure = {"label": "钱在活化", "tone": "orange", "reason": "剪刀差收敛、资金向金融市场迁移"}

    tsf = (items.get("tsf_yoy") or {}).get("value")
    if tsf is None:
        credit = {"label": "信用数据缺失", "tone": "green", "value": None}
        credit_side = None
    elif tsf < 8:
        credit = {"label": "信用收缩", "tone": "blue", "value": tsf}
        credit_side = "紧"
    elif tsf < 10:
        credit = {"label": "信用中性", "tone": "green", "value": tsf}
        credit_side = "中性"
    else:
        credit = {"label": "信用扩张", "tone": "amber", "value": tsf}
        credit_side = "松"

    active_title = None
    if liq_side == "松" and credit_side == "紧":
        active_title = "资金松 + 信用紧"
    elif liq_side == "松" and credit_side == "松":
        active_title = "资金松 + 信用松"
    elif liq_side == "紧" and heat_side == "热":
        active_title = "资金紧 + 热度高"
    elif liq_side == "紧" and heat_side == "冷":
        active_title = "资金紧 + 热度低"

    quadrants = []
    for quad in QUADRANTS:
        quadrants.append(dict(quad, active=(quad["title"] == active_title)))

    if active_title:
        quadrant = dict(next(q for q in QUADRANTS if q["title"] == active_title), active=True)
    else:
        quadrant = {
            "title": "混合状态",
            "brief": "当前不落在四个典型象限",
            "desc": (
                "资金面与信用/热度信号不一致（或数据不足），建议以单指标读数与趋势变化为准，"
                "等信号收敛后再下判断。"
            ),
            "tone": "green",
            "active": True,
        }

    liquidity["side"] = liq_side
    heat["side"] = heat_side
    credit["side"] = credit_side

    return {
        "liquidity": liquidity,
        "heat": heat,
        "credit": credit,
        "structure": structure,
        "quadrant": quadrant,
        "quadrants": quadrants,
        "memory": MEMORY_LINE,
        "summary": _summary_text(liquidity, credit, structure, tsf),
        "watch": WATCH_LIST,
        "liquidity_avg": liq_avg,
        "heat_avg": heat_avg,
        "structure_avg": struct_avg,
    }


def _summary_text(liquidity, credit, structure, tsf) -> str:
    """一句话综合：按当前三维标签拼出「宽货币 / 紧信用 / 部门结构」的定调。"""
    segs = []
    if liquidity.get("side") == "松":
        segs.append("宽货币（资金面平稳）")
    elif liquidity.get("side") == "紧":
        segs.append("资金面收紧")
    else:
        segs.append("货币中性")

    if credit.get("side") == "紧" and tsf is not None:
        segs.append("紧信用（社融存量同比 %s%%）" % tsf)
    elif credit.get("side") == "松" and tsf is not None:
        segs.append("信用扩张（社融存量同比 %s%%）" % tsf)
    else:
        segs.append(credit.get("label") or "信用待观察")

    segs.append("部门结构：%s" % (structure.get("label") or "待观察"))

    return (" + ".join(segs)
            + " → 盯「社融企稳 + 住户贷款转正 + 成交额与两融同向」，三者共振才是全面反转信号。")


# ---------------------------------------------------------------- 组装响应


def build_items(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把 {key: 读数} 加工成带状态判定与展示元数据的指标字典。"""
    items: Dict[str, Dict[str, Any]] = {}
    for ind in INDICATORS:
        key = ind["key"]
        row = raw.get(key) or {}
        value = row.get("value")
        decision = _decide(key, value)
        state, tone, level = decision if decision else (None, None, None)
        source = row.get("source") or "fallback"
        items[key] = {
            "key": key,
            "group": ind["group"],
            "name": ind["name"],
            "subtitle": ind["subtitle"],
            "unit": ind["unit"],
            "digits": ind["digits"],
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "source_detail": row.get("source_detail") or "",
            "value": value,
            "as_of": row.get("as_of"),
            "detail": row.get("detail") or {},
            "error": row.get("error"),
            "state": state,
            "tone": tone,
            "level": level,
            "meaning": ind["meaning"],
            "guide": ind["guide"],
            "bands": ind["bands"],
        }
    return items


def build_response(raw: Dict[str, Dict[str, Any]], params: Dict[str, Any]) -> Dict[str, Any]:
    """组装接口响应：三个维度的分组指标 + 参数 + 组合解读 + 宏观温度计。"""
    items = build_items(raw)

    groups = []
    for group_key in GROUP_ORDER:
        groups.append({
            "key": group_key,
            "title": GROUP_TITLES[group_key],
            "freq": GROUP_FREQS[group_key],
            "desc": GROUP_DESCS[group_key],
            "items": [items[k] for k in INDICATOR_KEYS if items[k]["group"] == group_key],
        })

    return {
        "status": "ok",
        "groups": groups,
        "params": params,
        "combination": compute_combination(items),
        "thermometer": macro_thermometer.compute_thermometer(items),
        "tones": TONES,
    }


def dump_detail(detail: Dict[str, Any]) -> str:
    """把 detail 字典序列化为 JSON 字符串，便于落库。"""
    try:
        return json.dumps(detail or {}, ensure_ascii=False)
    except Exception:
        return "{}"


def load_detail(text: Optional[str]) -> Dict[str, Any]:
    """从库里读出的 JSON 字符串还原为 detail 字典。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
