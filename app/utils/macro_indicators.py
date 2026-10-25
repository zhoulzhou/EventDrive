"""宏观盯盘清单（三个维度）数据获取、状态判定与组合解读。

三个维度、每维 3-4 项读数，共 10 项（2026-10-25 起按「NCD 版」方案重构）：

- ① 资金面（松 ↔ 紧，日频）：DR007 − 7天逆回购利率、1Y AAA 同业存单收益率、R001 − DR001
- ② 实体热度（冷 ↔ 热，月度）：制造业 PMI、PPI 同比、社融存量同比、M1 − M2 剪刀差
- ③ 信用 / 部门（钱的流向，月度）：企业中长贷同比多增、住户中长贷同比多增、财政存款当月环比

A股估值/情绪类读数（沪深300 PE/PB、ERP、成交额、两融）**不在本页**，
见「股票指标」页（app/utils/stock_temp.py）。

数据获取方式（全部自动，来源见各自 source 字段）：
- akshare（source="akshare"）
          · repo_rate_hist              → FDR001 / FDR007（即 DR001 / DR007 定盘价）、FR001（即 R001）
          · macro_china_pmi             → 制造业 PMI
          · macro_china_ppi             → PPI 当月同比
          · macro_china_money_supply    → M1 / M2 同比
- 中国货币网（source="chinamoney"，见 app/utils/ncd_rate.py）
          · 收盘收益率曲线 同业存单(AAA) 1Y → 银行中期负债成本
- 央行官网（source="pbc"，见 app/utils/pbc_data.py）
          · 《公开市场业务交易公告》      → 7天逆回购操作利率（每个交易日）
          · 《金融统计数据报告》          → 社融存量同比、分部门存贷款（含中长期贷款、财政存款）

⚠ 原「超储率」分项已于 2026-10-25 被 1Y AAA 同业存单替代：季频、要下载央行季报 PDF 解析，
既有季度滞后，线上发布工具又注入不了环境变量，不适合滚动刷新。

⚠ 模型的已知缺陷见 macro_thermometer.MODEL_LIMITS（四条，页面「模型边界与风控规则」同步展示）：
  ① 理财赎回 / 债市踩踏时 NCD 会脱离基本面流动性逻辑（资金面降置信度，人工复核）；
  ② 财政存款季末 / 年末季节性极强（按 SEASON_TAGS 打「季末脉冲 / 年末脉冲」标记）；
  ③ 信贷同比多增在 1 / 2 月受春节错月支配（打「春节月」标记，改看 1-2 月合并口径）；
  ④ 整套模型是景气扩张打分器，**不是金融风险模型**（居民杠杆 / 地产风险是外挂标签，不由分数表达）。
  条目内容只维护在 MODEL_LIMITS 一处，本文件只在 build_items 里调用 season_tag() 打标记。

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
    GROUP_HEAT: "② 实体热度（冷 ↔ 热）",
    GROUP_STRUCTURE: "③ 信用 / 部门（钱在谁手里）",
}

# 各维度的观察频率（放在维度标题右侧，提醒「日度盯异动、月度盯趋势」）
GROUP_FREQS = {
    GROUP_LIQUIDITY: "日频",
    GROUP_HEAT: "月频",
    GROUP_STRUCTURE: "月频 · 同比多增 / 环比",
}

GROUP_DESCS = {
    GROUP_LIQUIDITY: "钱贵不贵：看「价」（DR007 偏离、分层利差）与「银行负债成本」（1Y AAA 存单）",
    GROUP_HEAT: "经济跑不跑：看「先行」（PMI）、「价格」（PPI）、「信用水位」（社融）与「活化」（M1−M2）",
    GROUP_STRUCTURE: "钱在谁手里：企业扩产（中长贷）／居民杠杆（中长贷）／政府投放（财政存款），全用同比多增或环比变动",
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
        "key": "dr007_spread",
        "group": GROUP_LIQUIDITY,
        "name": "DR007 − 7天逆回购利率",
        "subtitle": "资金价格（5 日均）：银行资金价 vs 政策利率锚",
        "unit": "bp",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "DR007 是存款类机构（银行）7 天期质押式回购加权利率，是央行重点观察的"
            "\"政策利率 → 市场利率\"传导指标；7 天逆回购利率是央行公开市场操作的锚"
            "（当前锚 1.40%）。两者之差即银行拿钱的成本相对央行合意水平偏离了多少。"
        ),
        "guide": (
            "看\"价\"。负偏离=资金比政策利率还便宜，说明钱多；正偏离持续扩大=银行体系缺钱，"
            "缴税、政府债缴款高峰时常出现；+100bp 以上属钱荒级别（2013 年 6 月、2020 年末）。"
        ),
        "bands": [
            {"range": "低于 −20bp", "state": "松，钱富余", "tone": "blue"},
            {"range": "−20 ~ −10bp", "state": "偏松", "tone": "cyan"},
            {"range": "±10bp 内", "state": "央行合意区间，平稳", "tone": "green"},
            {"range": "+10 ~ +20bp", "state": "略偏紧", "tone": "amber"},
            {"range": "+20 ~ +30bp", "state": "边际偏紧", "tone": "amber"},
            {"range": "+30 ~ +50bp", "state": "偏紧", "tone": "orange"},
            {"range": "+50bp 以上持续", "state": "明显紧张（缴税 / 政府债缴款高峰）", "tone": "orange"},
            {"range": "+100bp 以上", "state": "钱荒级别（2013、2020 年末）", "tone": "red"},
        ],
    },
    {
        "key": "ncd_1y_aaa",
        "group": GROUP_LIQUIDITY,
        "name": "1Y AAA 同业存单收益率",
        "subtitle": "银行中期负债成本（5 日均线打分）",
        "unit": "%",
        "digits": 3,
        "source": "auto",
        "meaning": (
            "AAA 级同业存单 1 年期到期收益率，取中债估值曲线（中国货币网「收盘收益率曲线」）。"
            "同业存单是银行主动负债的主要工具，1Y 存单收益率≈银行中期负债成本中枢："
            "存单上行 → 银行揽存压力大、放贷 / 买债意愿收敛；存单下行 → 负债端压力缓解，"
            "信用供给更容易打开。"
        ),
        "guide": (
            "看「银行借钱的成本」。用 5 日均线打分，过滤缴税、跨季、月末脉冲。"
            "1.30~1.60% 属中性偏松；>1.95% 说明揽存压力明显；<1.00% 属历史极低位（打分饱和在 100）。"
            "⚠ 官方没有日频的「同业存单发行利率」曲线，本项以同期限到期收益率代理"
            "（发行利率 ≈ 到期收益率 + 一级溢价，点差通常几个 bp）。"
        ),
        "bands": [
            {"range": "< 1.00%", "state": "极低位，负债端极宽松", "note": "打分饱和 100", "tone": "blue"},
            {"range": "1.00 ~ 1.30%", "state": "偏低位", "tone": "cyan"},
            {"range": "1.30 ~ 1.60%", "state": "中性偏松", "note": "锚点中枢", "tone": "green"},
            {"range": "1.60 ~ 1.95%", "state": "偏高，揽存压力显现", "tone": "amber"},
            {"range": "> 1.95%", "state": "高位，银行负债端吃紧", "tone": "red"},
        ],
    },
    {
        "key": "r001_dr001",
        "group": GROUP_LIQUIDITY,
        "name": "R001 − DR001",
        "subtitle": "非银分层（5 日均）",
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
            "打分只用 headline；新订单分项作为辅助观测（用来区分被动补库还是真实需求），"
            "本页未接入、不进打分。"
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
            "看工业品的\"体温\"。适度 PPI 上行利好企业毛利；只有高通胀（同比 6% 以上）才会"
            "约束货币政策宽松空间，所以过热阈值放在 +6%/+9% 而不是 +2%/+4%——"
            "避免温和涨价就被判成亢奋。持续深度为负 = 工业通缩。"
        ),
        "bands": [
            {"range": "> 9%", "state": "过热，成本挤压中下游", "tone": "red"},
            {"range": "6 ~ 9%", "state": "偏热、供需两旺", "tone": "amber"},
            {"range": "3 ~ 6%", "state": "温和涨价、毛利修复", "tone": "lime"},
            {"range": "0 ~ 3%", "state": "弱修复", "tone": "lime"},
            {"range": "−2 ~ 0%", "state": "通缩阴影", "tone": "cyan"},
            {"range": "< −2%", "state": "深度通缩（2023–2024 常态）", "tone": "blue"},
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
        "group": GROUP_HEAT,
        "name": "M1 − M2 剪刀差",
        "subtitle": "货币活化（实体热度分项）",
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
            "预期改善；持续为负且扩大 = 钱趴在定期存款，经营周转弱。"
            "要和 ③ 信用/部门一起读：总量没变但换了口袋，也算结构变化。"
        ),
        "bands": [
            {"range": "> 0", "state": "资金活化强、经济热度高", "note": "2016–17 牛市区间", "tone": "red"},
            {"range": "0 ~ −3%", "state": "中性", "tone": "green"},
            {"range": "−3 ~ −6%", "state": "偏冷", "tone": "cyan"},
            {"range": "< −6%", "state": "深度偏冷", "note": "2024 年曾到约 −8%", "tone": "blue"},
        ],
    },
    # ------------------------------------------------------------ ③ 信用 / 部门
    {
        "key": "corp_ml_loan_yoy",
        "group": GROUP_STRUCTURE,
        "name": "企业中长贷 · 同比多增",
        "subtitle": "企业资本开支意愿（当月 vs 去年同月）",
        "unit": "亿元",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "企（事）业单位中长期贷款**当月增量**，减去年同月的当月增量，得到「同比多增 / 少增」。"
            "中长贷主要对应设备更新、厂房扩建、资本开支，是内生复苏最硬的信号；"
            "本模块里权重最高（40%）。数据来自央行《金融统计数据报告》的分部门贷款，"
            "报告只披露年初至今累计数，当月增量由相邻两期累计相减得到。"
        ),
        "guide": (
            "高分 = 企业主动扩产；低分 = 资本开支意愿低迷。用同比多增而不是绝对额，"
            "是为了绕开春节、年末的季节性。⚠ 企业短贷、票据冲量不算资本开支，"
            "只作为辅助观测（见下方取数与口径表），不进打分。"
        ),
        "bands": [
            {"range": "≥ +4000 亿", "state": "显著多增，扩产明确", "tone": "red"},
            {"range": "+2000 ~ +4000 亿", "state": "同比多增", "tone": "amber"},
            {"range": "−2000 ~ +2000 亿", "state": "基本持平", "tone": "green"},
            {"range": "−4000 ~ −2000 亿", "state": "明显少增", "tone": "cyan"},
            {"range": "≤ −4000 亿", "state": "大幅少增，资本开支收缩", "tone": "blue"},
        ],
    },
    {
        "key": "household_ml_loan_yoy",
        "group": GROUP_STRUCTURE,
        "name": "住户中长贷 · 同比多增",
        "subtitle": "居民杠杆 / 地产需求（当月 vs 去年同月）",
        "unit": "亿元",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "住户中长期贷款（≈ 房贷为主）当月增量减去年同月，代表居民部门杠杆与地产需求变化。"
            "双重属性：复苏初期回暖是利好；居民杠杆已在高位还持续大幅多增，则属结构风险。"
            "风险提示单独外挂标签，不修改打分函数。"
        ),
        "guide": (
            "看符号与量级：同比多增 = 居民重新借钱、地产销售与消费信心修复；"
            "持续大幅少增 = 居民提前还贷、缩表。绝对值单看会误导——房贷有很强的季节性，"
            "所以要跟去年同月比。"
        ),
        "bands": [
            {"range": "≥ +3000 亿", "state": "显著多增，居民加杠杆", "tone": "red"},
            {"range": "+1500 ~ +3000 亿", "state": "同比多增", "tone": "amber"},
            {"range": "−1500 ~ +1500 亿", "state": "基本持平", "tone": "green"},
            {"range": "−3000 ~ −1500 亿", "state": "明显少增", "tone": "cyan"},
            {"range": "≤ −3000 亿", "state": "大幅少增，提前还贷 / 缩表", "tone": "blue"},
        ],
    },
    {
        "key": "fiscal_deposit_mom",
        "group": GROUP_STRUCTURE,
        "name": "财政存款 · 当月环比",
        "subtitle": "政府投放节奏（财政存款减少 = 钱拨到实体）",
        "unit": "亿元",
        "digits": 0,
        "source": "auto",
        "meaning": (
            "财政性存款当月增量相对上月的变动，是政府部门的代理变量（补上原模型缺失的政府部门）。"
            "财政存款增加 = 资金上缴国库、抽离实体；财政存款减少 = 国库拨付、专项债资金落地、"
            "财政花钱托底实体。数据同样来自《金融统计数据报告》的分部门存款。"
        ),
        "guide": (
            "方向与其它两项相反：<b>减少才加分</b>。环比减少越多，说明财政投放越积极。"
            "季节性极强（季末、年末缴税），所以看环比变动而不是绝对水平，"
            "并结合 ② 的社融、PMI 判断是「真投放」还是「缴税扰动」。"
        ),
        "bands": [
            {"range": "减少 ≥ 3000 亿", "state": "财政大幅投放", "tone": "red"},
            {"range": "减少 1500 ~ 3000 亿", "state": "投放加快", "tone": "amber"},
            {"range": "±1500 亿内", "state": "中性", "tone": "green"},
            {"range": "增加 1500 ~ 3000 亿", "state": "资金回笼", "tone": "cyan"},
            {"range": "增加 ≥ 3000 亿", "state": "大幅上缴，抽离实体", "tone": "blue"},
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

# 7 天逆回购利率：央行政策利率锚，用于计算 DR007 偏离
# （自动抓取自央行《公开市场业务交易公告》）
PARAM_REVERSE_REPO = "reverse_repo_rate"
PARAM_META = {
    PARAM_REVERSE_REPO: {
        "name": "7天逆回购利率（政策利率锚）",
        "unit": "%",
        "digits": 2,
        "default": 1.40,
        "hint": "央行 7 天期公开市场逆回购操作利率，用于计算 DR007 偏离。自动抓取自《公开市场业务交易公告》。",
    },
}

# 抓取失败且库中无记录时的兜底读数（保证页面有数可展示，source 标记为 fallback）
FALLBACK_DEFAULTS: Dict[str, Any] = {
    "tsf_yoy": {"value": 7.2, "as_of": "兜底默认值"},
    PARAM_REVERSE_REPO: {"value": 1.40, "as_of": "兜底默认值"},
}

# 自动抓取键：akshare / 中国货币网 / 央行官网（无手工入口，全部自动）
AK_KEYS = ["dr007_spread", "r001_dr001", "pmi", "m1_m2", "ppi_yoy"]
NCD_KEYS = ["ncd_1y_aaa"]
PBC_KEYS = ["tsf_yoy", "corp_ml_loan_yoy", "household_ml_loan_yoy", "fiscal_deposit_mom",
            PARAM_REVERSE_REPO]
AUTO_KEYS = AK_KEYS + NCD_KEYS + PBC_KEYS

# 有效自动来源；库中记录的 source 不在此集合内（如历史遗留的 manual）即视为待刷新
AUTO_SOURCES = {"akshare", "pbc", "chinamoney"}

# 数据来源展示标签
SOURCE_LABELS = {
    "akshare": "akshare",
    "chinamoney": "中国货币网",
    "pbc": "央行",
    "fallback": "默认值",
}

# 宏观温度计打卡键：合成值（资金面×25% + 实体热度×45% + 信用/部门×30%），
# 不属于任何一张指标表，故单独写进历史表，供「宏观趋势」页画温度计曲线。
# detail 里存三个子温度计的分数与价差，前端据此画分层曲线。
THERMOMETER_KEY = "macro_thermometer"

# 历史接口允许查询的全部键（指标 + 参数锚 + 图表专用序列 + 温度计打卡）
HISTORY_KEYS = (set(INDICATOR_KEYS) | {PARAM_REVERSE_REPO} | set(SERIES_EXTRA_KEYS)
                | {THERMOMETER_KEY})

# 各键的中文名（含政策利率锚与图表专用序列）
NAME_OF_KEY = {ind["key"]: ind["name"] for ind in INDICATORS}
NAME_OF_KEY[PARAM_REVERSE_REPO] = PARAM_META[PARAM_REVERSE_REPO]["name"]
NAME_OF_KEY.update(SERIES_EXTRA_KEYS)
NAME_OF_KEY[THERMOMETER_KEY] = "宏观温度计"

# 图表面板「三组一定位」用到的序列键（供 /api/macro/series 组装）
# NCD（1Y AAA 存单）是日频，与 DR007 / R001 同属资金面日频组
CHART_DAILY_KEYS = ["dr007_spread", "r001_dr001", "ncd_1y_aaa"]
CHART_MONTHLY_KEYS = ["pmi", "ppi_yoy", "tsf_yoy", "m1_yoy", "m2_yoy", "m1_m2"]
CHART_KEYS = CHART_DAILY_KEYS + CHART_MONTHLY_KEYS

# ---------------------------------------------------------------- 状态判定


def _decide(key: str, value: Optional[float]):
    """按指标阈值把读数判定为 (状态标签, 色调, 严重度等级)。

    资金面：等级 0=最松 → 5=最紧；②③：等级 0=最冷/最淤积 → 4=最热/最活化。
    """
    if value is None:
        return None
    v = float(value)

    if key == "dr007_spread":  # bp
        if v < -20:
            return ("松，钱富余", "blue", 0)
        if v < -10:
            return ("偏松", "cyan", 1)
        if v <= 10:
            return ("央行合意区间，平稳", "green", 2)
        if v < 20:
            return ("略偏紧", "amber", 3)
        if v <= 30:
            return ("边际偏紧", "amber", 3)
        if v < 50:
            return ("偏紧", "orange", 4)
        if v < 100:
            return ("明显紧张", "orange", 4)
        return ("钱荒级别", "red", 5)

    if key == "ncd_1y_aaa":  # %（1Y AAA 同业存单收益率，5 日均；锚点见 macro_thermometer.SUB_INDICES）
        if v < 1.00:
            return ("极低位，负债端极宽松", "blue", 0)
        if v < 1.30:
            return ("偏低位", "cyan", 1)
        if v < 1.60:
            return ("中性偏松", "green", 2)
        if v < 1.95:
            return ("偏高，揽存压力显现", "amber", 3)
        if v < 2.30:
            return ("高位，负债端吃紧", "orange", 4)
        return ("极高位，信用供给受抑", "red", 5)

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
        if v > 9:
            return ("过热，成本挤压中下游", "red", 4)
        if v >= 6:
            return ("偏热、供需两旺", "amber", 3)
        if v >= 3:
            return ("温和涨价、毛利修复", "lime", 2)
        if v >= 0:
            return ("弱修复", "lime", 2)
        if v >= -2:
            return ("通缩阴影", "cyan", 1)
        return ("深度通缩", "blue", 0)

    # ---------------- ③ 信用 / 部门（0=收缩/抽水 → 4=扩张/投放）
    if key == "corp_ml_loan_yoy":  # 亿元（当月同比多增）
        if v >= 4000:
            return ("显著多增，扩产明确", "red", 4)
        if v >= 2000:
            return ("同比多增", "amber", 3)
        if v >= -2000:
            return ("基本持平", "green", 2)
        if v >= -4000:
            return ("明显少增", "cyan", 1)
        return ("大幅少增，资本开支收缩", "blue", 0)

    if key == "household_ml_loan_yoy":  # 亿元（当月同比多增）
        if v >= 3000:
            return ("显著多增，居民加杠杆", "red", 4)
        if v >= 1500:
            return ("同比多增", "amber", 3)
        if v >= -1500:
            return ("基本持平", "green", 2)
        if v >= -3000:
            return ("明显少增", "cyan", 1)
        return ("大幅少增，提前还贷 / 缩表", "blue", 0)

    if key == "fiscal_deposit_mom":  # 亿元（当月环比变动，减少=投放=高分）
        if v <= -3000:
            return ("财政大幅投放", "red", 4)
        if v <= -1500:
            return ("投放加快", "amber", 3)
        if v <= 1500:
            return ("中性", "green", 2)
        if v <= 3000:
            return ("资金回笼", "cyan", 1)
        return ("大幅上缴，抽离实体", "blue", 0)

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


# 利率类分项的统一降噪窗口：5 日滚动均值（过滤缴税、跨季、月末脉冲）
REPO_MEAN_WINDOW = 5


def _fetch_repo_rates(ak) -> Dict[str, Any]:
    """抓取最近的银行间回购定盘利率（FDR001/FDR007 即 DR001/DR007，FR001 即 R001）。

    repo_rate_hist 要求起止日期在一个月内，故取本月 1 日至今天；若本月尚无数据
    （月初），回退到上一个月区间重试。

    返回 {"date": 最新交易日, "days": 均线样本天数,
          "last": {...当日值...}, "mean5": {...5 日均值...}}。
    资金面温度计用的是 mean5（5 日滚动均值），当日值只用于 detail 展示 ——
    单日缴税 / 跨季 / 月末脉冲会把裸读数打得很脏。
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
            df = ak.repo_rate_hist(
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
        rows = df.tail(REPO_MEAN_WINDOW)
        last = df.iloc[-1]

        def _last(key):
            v = last.get(key)
            return None if v is None or v != v else float(v)

        def _mean(key):
            vals = [float(x) for x in rows[key].tolist() if x is not None and x == x] \
                if key in rows.columns else []
            return round(mean(vals), 4) if vals else None

        return {
            "date": str(last["date"])[:10],
            "days": len(rows),
            "last": {"FR001": _last("FR001"), "FDR001": _last("FDR001"),
                     "FDR007": _last("FDR007")},
            "mean5": {"FR001": _mean("FR001"), "FDR001": _mean("FDR001"),
                      "FDR007": _mean("FDR007")},
        }
    raise RuntimeError("回购定盘利率抓取失败: %s" % (last_error or "区间内无数据"))


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
    df = ak.macro_china_money_supply()
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


def _import_ncd():
    """延迟导入存单模块；不可用时返回 None。"""
    try:
        from app.utils import ncd_rate
        return ncd_rate
    except Exception as exc:  # pragma: no cover - 环境相关
        logger.warning("存单利率模块不可用: %s", exc)
        return None


def _fetch_ncd_rate(ncd) -> Dict[str, Any]:
    """1Y AAA 同业存单收益率：value = 5 日均值（%），as_of = 最新交易日。

    打分用 5 日均线，故 detail 里同时给「当日值」与「5 日均值」，
    卡片上显示的 as_of 是最新交易日而不是均值区间的中点。
    """
    item = ncd.fetch_ncd_1y()
    return {
        "value": item["value"],
        "as_of": item["as_of"],
        "detail": item.get("detail") or {},
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
    hints           ：各指标已落库的 detail；现仅保留参数以兼容调用方
                      （原「超储率报告期未变则跳过下载 PDF」的逻辑已随分项下线）。
    """
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

    # ---------------- 中国货币网：1Y AAA 同业存单收益率（替代原超储率）
    ncd = _import_ncd()
    if ncd is None:
        for key in NCD_KEYS:
            _fail(key, "chinamoney", RuntimeError("存单利率模块不可用"))
    else:
        try:
            item = _fetch_ncd_rate(ncd)
            result["ncd_1y_aaa"] = _item(item["value"], item["as_of"], "chinamoney",
                                         item.get("detail"))
        except Exception as exc:
            _fail("ncd_1y_aaa", "chinamoney", exc)

    # ---------------- 央行官网：逆回购利率 / 社融存量同比 / 分部门存贷款（信用·部门三项）
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
            credit = pbc.fetch_credit_structure()
            for key in ("corp_ml_loan_yoy", "household_ml_loan_yoy", "fiscal_deposit_mom"):
                node = (credit.get("items") or {}).get(key) or {}
                value = node.get("value")
                if value is None:
                    _fail(key, "pbc", RuntimeError("分部门贷款 / 财政存款解析不到该项"))
                    continue
                detail = dict(node.get("detail") or {})
                # 温度计「取数与口径」表要读整份明细，故随该项一并落库
                detail["_全量口径"] = credit.get("detail") or {}
                result[key] = _item(value, node.get("as_of"), "pbc", detail)
        except Exception as exc:
            for key in ("corp_ml_loan_yoy", "household_ml_loan_yoy", "fiscal_deposit_mom"):
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
        _fail("dr007_spread", "akshare", exc)
        _fail("r001_dr001", "akshare", exc)

    if repo is not None:
        last = repo.get("last") or {}
        avg = repo.get("mean5") or {}
        fdr007 = avg.get("FDR007")
        fdr001 = avg.get("FDR001")
        fr001 = avg.get("FR001")

        if fdr007 is None:
            _fail("dr007_spread", "akshare", RuntimeError("缺少 FDR007 读数"))
        else:
            result["dr007_spread"] = _item(
                round((fdr007 - anchor) * 100, 1), repo["date"], "akshare",
                {"DR007 当日": last.get("FDR007"), "DR007 5日均": fdr007,
                 "逆回购利率": anchor, "均线样本天数": repo.get("days")},
            )

        if fdr001 is None or fr001 is None:
            _fail("r001_dr001", "akshare", RuntimeError("缺少 FR001 / FDR001 读数"))
        else:
            result["r001_dr001"] = _item(
                round((fr001 - fdr001) * 100, 1), repo["date"], "akshare",
                {"R001 当日": last.get("FR001"), "DR001 当日": last.get("FDR001"),
                 "R001 5日均": fr001, "DR001 5日均": fdr001,
                 "均线样本天数": repo.get("days")},
            )

    for key, fetcher in (
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
            "DR007 平 ≠ 经济好，必须拿社融、PMI 证伪。"
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
    {"key": "corp_ml_loan_yoy", "label": "企业中长贷转「同比多增」（企业重新扩产）"},
    {"key": "household_ml_loan_yoy", "label": "住户中长贷转「同比多增」（居民重新借钱）"},
]


def _avg(values: List[int]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(mean(vals), 2) if vals else None


def compute_combination(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """由三个维度汇总出资金面松紧、实体热度与信用/部门状态，并给出四象限归属。"""
    # ① 资金面：价格偏离 + 银行负债成本 + 分层利差
    liq_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("dr007_spread", "ncd_1y_aaa", "r001_dr001")
    ])
    # ② 实体热度：先行（PMI）+ 价格（PPI）+ 信用水位（社融）+ 活化（M1−M2）
    heat_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("pmi", "tsf_yoy", "ppi_yoy", "m1_m2")
    ])
    # ③ 信用/部门：企业资本开支 + 居民杠杆 + 财政投放
    struct_avg = _avg([
        (items.get(k) or {}).get("level")
        for k in ("corp_ml_loan_yoy", "household_ml_loan_yoy", "fiscal_deposit_mom")
    ])

    if liq_avg is None:
        liquidity = {"label": "数据不足", "tone": "green", "reason": "资金面指标缺失"}
        liq_side = None
    elif liq_avg < 2.5:
        liquidity = {"label": "资金松", "tone": "green", "reason": "价格偏离、存单、分层利差均不紧"}
        liq_side = "松"
    elif liq_avg < 3.5:
        liquidity = {"label": "资金中性", "tone": "amber", "reason": "资金面接近均衡、边际有扰动"}
        liq_side = "中性"
    else:
        liquidity = {"label": "资金紧", "tone": "red", "reason": "资金价格偏高或存单抬升"}
        liq_side = "紧"

    if heat_avg is None:
        heat = {"label": "数据不足", "tone": "green", "reason": "经济热度指标缺失"}
        heat_side = None
    elif heat_avg < 1.5:
        heat = {"label": "热度偏低", "tone": "cyan", "reason": "景气、价格、信用、活化整体偏冷"}
        heat_side = "冷"
    elif heat_avg < 2.5:
        heat = {"label": "热度中性", "tone": "green", "reason": "冷热指标互有强弱"}
        heat_side = "中性"
    else:
        heat = {"label": "热度偏高", "tone": "red", "reason": "景气、价格、信用、活化整体偏热"}
        heat_side = "热"

    if struct_avg is None:
        structure = {"label": "数据不足", "tone": "green", "reason": "信用/部门指标缺失"}
    elif struct_avg < 1.5:
        structure = {"label": "收缩·内生动力弱", "tone": "blue",
                     "reason": "企业中长贷少增、居民缩表、财政未发力"}
    elif struct_avg < 2.5:
        structure = {"label": "中性", "tone": "green", "reason": "企业/居民/财政互有强弱"}
    else:
        structure = {"label": "扩张·钱在落地", "tone": "orange",
                     "reason": "企业中长贷多增、居民加杠杆、财政投放"}

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
    """一句话综合：按当前三维标签拼出「宽货币 / 紧信用 / 信用部门」的定调。"""
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

    segs.append("信用/部门：%s" % (structure.get("label") or "待观察"))

    return (" + ".join(segs)
            + " → 盯「社融企稳 + 企业中长贷多增 + 住户中长贷多增」，三者共振才是全面反转信号。")


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
            # 季节标记（季末财政脉冲 / 春节月）：见 macro_thermometer.SEASON_TAGS，
            # 纯提示不参与打分——对应 MODEL_LIMITS 里的 ②③ 两条已知缺陷
            "season_tag": macro_thermometer.season_tag(key, row.get("as_of")),
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
