"""股票指标（A 股冷热三层温度计）数据获取、状态判定与综合温度合成。

体系来自「A 股冷热要分三层看」的方法论：估值层（贵不贵）、情绪层（亢奋不亢奋）、
资金层（钱进不进），最后合成一个 0-100 的市场温度计，全部用历史分位说话。

四组指标：
- 估值层（valuation）：沪深300 PE-TTM / 沪深300 PB / ERP 股债性价比 / 破净率
- 情绪层（sentiment）：两市成交额 / 全A换手率 / 两融占流通市值 / 市场宽度
- 资金层（capital）  ：公募仓位 / 股票ETF份额 / 新基金发行 / 产业资本净增持
- 结构  （structure）：科创50 vs 创业板 分位差

数据获取方式（全部自动，来源统一记为 source="akshare"，实际站点见各指标 note）：
- 乐咕乐股（legulegu，经 akshare）
      · stock_index_pe_lg / stock_index_pb_lg        → 沪深300 PE-TTM / PB（月频，2005 年起）
      · stock_a_below_net_asset_statistics           → 全部A股破净率（日频，2005 年起）
      · fund_stock_position_lg                       → 股票型基金仓位（周频，2008 年起）
- 交易所官网（经 akshare，无反爬、按日可取，用于回填）
      · stock_sse_deal_daily / stock_szse_summary    → 沪/深 股票成交金额与流通市值
- 中债 / 新浪（经 akshare）
      · bond_zh_us_rate                              → 中国 10 年期国债收益率
- 东财数据中心（datacenter-web.eastmoney.com，直连）
      · 融资融券账户信息                              → 两融余额（stock_margin_account_info）
      · RPT_SHARE_HOLDER_INCREASE                    → 重要股东增减持（产业资本净增持）
- 新浪 / 东财（经 akshare）
      · stock_market_activity_legu                   → 涨跌家数（市场宽度）
      · fund_scale_open_sina                         → 宽基股票ETF 份额快照
      · fund_new_found_em                            → 新成立基金募集份额
      · stock_zh_index_daily                         → 科创50 / 创业板指 收盘

任一项抓取失败时不做异常抛出：该指标返回 error，页面按「数据缺失」展示并保留上一次
成功读数，不影响其余指标与页面渲染。
"""
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 分组与色调

GROUP_VALUATION = "valuation"
GROUP_SENTIMENT = "sentiment"
GROUP_CAPITAL = "capital"
GROUP_STRUCTURE = "structure"

GROUP_TITLES = {
    GROUP_VALUATION: "① 估值层 · 贵不贵",
    GROUP_SENTIMENT: "② 情绪层 · 亢奋还是低迷",
    GROUP_CAPITAL: "③ 资金层 · 钱进不进",
    GROUP_STRUCTURE: "④ 结构 · 热度在哪个市场",
}

GROUP_SUBTITLES = {
    GROUP_VALUATION: "月频慢变量 · 决定能不能长期拿",
    GROUP_SENTIMENT: "日频快变量 · 决定短线热不热",
    GROUP_CAPITAL: "周频/月频 · 决定行情有没有持续性",
    GROUP_STRUCTURE: "观察分化：同一个 A 股，热度可能不在一个市场",
}

GROUP_TONES = {
    GROUP_VALUATION: "blue",
    GROUP_SENTIMENT: "amber",
    GROUP_CAPITAL: "green",
    GROUP_STRUCTURE: "violet",
}

TONES = {
    "blue": "#3b82f6",
    "cyan": "#06b6d4",
    "green": "#22c55e",
    "lime": "#84cc16",
    "amber": "#f59e0b",
    "orange": "#f97316",
    "red": "#ef4444",
    "violet": "#8b5cf6",
    "slate": "#64748b",
}

SOURCE_LABELS = {
    "akshare": "自动抓取",
    "fallback": "兜底参考值",
}

# ---------------------------------------------------------------- 温度计分层

TEMP_LAYERS = [
    {
        "key": GROUP_VALUATION,
        "name": "估值温度",
        "weight": 0.50,
        "tone": "blue",
        "desc": "PE 分位 40% + ERP 30% + 破净率 30%。慢变量，定方向。",
    },
    {
        "key": GROUP_SENTIMENT,
        "name": "情绪温度",
        "weight": 0.30,
        "tone": "amber",
        "desc": "成交额分位 50% + 换手率分位 30% + 两融占流通市值 20%。快变量，定情绪。",
    },
    {
        "key": GROUP_CAPITAL,
        "name": "资金温度",
        "weight": 0.20,
        "tone": "green",
        "desc": "公募仓位 30% + 股票ETF份额 25% + 新基金发行 25% + 产业资本 20%。资金变量，定底气。",
    },
]

# 综合温度 = 估值 ×50% + 情绪 ×30% + 资金 ×20%
TEMP_ZONES = [
    {"lo": 0, "hi": 20, "state": "冰点 · 布局区", "tone": "blue"},
    {"lo": 20, "hi": 40, "state": "偏冷", "tone": "cyan"},
    {"lo": 40, "hi": 60, "state": "中性", "tone": "lime"},
    {"lo": 60, "hi": 80, "state": "偏热", "tone": "orange"},
    {"lo": 80, "hi": 100.01, "state": "亢奋 · 风险区", "tone": "red"},
]

# ---------------------------------------------------------------- 分位窗口

# 各频率下「十年分位」对应的样本点数（月 120 / 周 520 / 日 2500）
PCT_WINDOW_POINTS = {"M": 120, "W": 520, "D": 2500}
FREQ_LABELS = {"M": "近十年（120 个月）", "W": "近十年（520 周）", "D": "近十年（2500 个交易日）"}

# 历史表里图表用到的全部键
CHART_KEYS = [
    "hs300_pe", "hs300_pb", "erp", "below_na",
    "turnover_amt", "turnover_rate", "margin", "total_float_mcap", "breadth",
    "fund_position", "new_fund", "industry_capital", "etf_share",
    "structure_div", "temperature",
]

# ---------------------------------------------------------------- 指标清单
#
# bands 的元素：{lo, hi, range, state, tone, note?, score}
#   - lo/hi 为该档的数值下/上界（None 表示无界），用于判定「当前落在哪一档」
#   - score 为该档折算成 0-100「热度分」的取值（越高越热），当分位拿不到时作为兜底
# judge 决定用什么值去匹配 bands：
#   "value" 用指标读数本身；"percentile" 用十年分位；"rate" 用 detail 里的变化率

INDICATORS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------ ① 估值层
    {
        "key": "hs300_pe",
        "group": GROUP_VALUATION,
        "name": "沪深300 PE-TTM",
        "subtitle": "宽基估值：贵不贵",
        "unit": "倍",
        "digits": 2,
        "temp_layer": GROUP_VALUATION,
        "temp_weight": 0.40,
        "temp_invert": False,
        "freq": "月频",
        "judge": "percentile",
        "meaning": (
            "沪深300 指数滚动市盈率（PE-TTM，市值加权），是宽基指数最常用的估值锚。"
            "这里不看绝对值，只看它在过去十年分布里的位置（分位）。"
        ),
        "guide": (
            "十年分位 <20% 极冷（便宜）、>80% 极热（贵）。"
            "⚠ 看 PE 先拆分母：盈利下行会「被动抬升」PE——分母（E）缩水而不是分子（P）涨，"
            "这种「贵」是假贵，必须配 PB 交叉验证。"
        ),
        "warning": "PE 分位偏高时先看是不是盈利下行「分母收缩」造成的，务必配 PB 一起读。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "极冷 · 便宜", "tone": "blue", "score": 10},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏冷", "tone": "cyan", "score": 30},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏热", "tone": "orange", "score": 70},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "极热 · 贵", "tone": "red", "score": 90},
        ],
    },
    {
        "key": "hs300_pb",
        "group": GROUP_VALUATION,
        "name": "沪深300 PB",
        "subtitle": "PB 交叉验证 PE",
        "unit": "倍",
        "digits": 2,
        "temp_layer": GROUP_VALUATION,
        "temp_weight": 0.0,
        "temp_invert": False,
        "freq": "月频",
        "judge": "percentile",
        "meaning": (
            "沪深300 指数市净率（PB）。净资产（B）比盈利（E）稳定得多，"
            "所以 PB 是判断「估值底部有没有支撑」的交叉验证工具。"
        ),
        "guide": (
            "PB 分位与 PE 分位背离时，以 PB 为准：PE 高 + PB 低 = 盈利周期底部"
            "（分母收缩造成的假贵），而不是真的贵。"
        ),
        "warning": "PE 与 PB 分位背离时，PB 更可信——净资产比盈利稳定。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "极冷 · 估值底部", "tone": "blue", "score": 10},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏低", "tone": "cyan", "score": 30},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏高", "tone": "orange", "score": 70},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "极热 · 贵", "tone": "red", "score": 90},
        ],
    },
    {
        "key": "erp",
        "group": GROUP_VALUATION,
        "name": "ERP 股债性价比",
        "subtitle": "1/PE − 10 年期国债收益率",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_VALUATION,
        "temp_weight": 0.30,
        "temp_invert": True,
        "freq": "月频",
        "judge": "percentile",
        "meaning": (
            "股权风险溢价 ERP = 1/PE − 10 年期国债收益率，衡量「买股票相对买国债多拿多少赔率」。"
            "数值越高 = 股票越便宜、赔率越好。"
        ),
        "guide": (
            "看十年分位：>80% 说明赔率仍在好的一侧（偏冷、便宜）；<20% 说明股票相对债券已无性价比"
            "（偏热）。历史上 ERP 进入极低分位后，权益的中期收益普遍不佳。"
        ),
        "warning": "ERP 是「相对」指标：无风险利率大幅下行时，ERP 走高不等于股票立刻会涨，只说明赔率改善。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "贵 · 相对债券无性价比", "tone": "red", "score": 88},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏贵", "tone": "orange", "score": 70},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏便宜 · 偏冷", "tone": "cyan", "score": 30},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "赔率好 · 冷", "tone": "blue", "score": 12},
        ],
    },
    {
        "key": "below_na",
        "group": GROUP_VALUATION,
        "name": "破净率",
        "subtitle": "全部 A 股股价跌破每股净资产的比例",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_VALUATION,
        "temp_weight": 0.30,
        "temp_invert": True,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "全部 A 股中「股价 < 每股净资产」的公司占比（含 B 股口径为全部 A 股）。"
            "破净是深度低估、悲观定价的集中体现，是典型的底部区域温度计。"
        ),
        "guide": ">10% 属历史底部区；3 ~ 6% 中性；<3% 说明市场已经很难找到便宜货，偏热。",
        "bands": [
            {"lo": 10, "hi": None, "range": "> 10%", "state": "历史底部区", "tone": "blue", "score": 12},
            {"lo": 6, "hi": 10, "range": "6 ~ 10%", "state": "偏冷", "tone": "cyan", "score": 30},
            {"lo": 3, "hi": 6, "range": "3 ~ 6%", "state": "中性", "tone": "lime", "score": 52},
            {"lo": 1, "hi": 3, "range": "1 ~ 3%", "state": "偏热", "tone": "orange", "score": 72},
            {"lo": None, "hi": 1, "range": "< 1%", "state": "极热 · 无便宜货", "tone": "red", "score": 88},
        ],
    },

    # ------------------------------------------------------------ ② 情绪层
    {
        "key": "turnover_amt",
        "group": GROUP_SENTIMENT,
        "name": "两市成交额",
        "subtitle": "沪 + 深 股票成交金额（交易所官网口径）",
        "unit": "亿元",
        "digits": 0,
        "temp_layer": GROUP_SENTIMENT,
        "temp_weight": 0.50,
        "temp_invert": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "沪市 + 深市股票成交金额合计，是市场活跃度最直接的度量。"
            "取自上交所《每日概况》与深交所《市场总貌》官方口径，非行情商估算值。"
        ),
        "guide": (
            "成交额只说明「热不热」，不说明「贵不贵」：<7000 亿属地量冰点；"
            "2 万亿以上是亢奋区；3 万亿以上属极端。地量往往对应底部区域的成交量特征。"
        ),
        "warning": "成交额 2 万亿 ≠ 市场便宜。它是情绪温度，不是估值温度，两层必须分开看。",
        "bands": [
            {"lo": None, "hi": 7000, "range": "< 7000 亿", "state": "地量 · 冰点", "tone": "blue", "score": 8},
            {"lo": 7000, "hi": 12000, "range": "7000 ~ 12000 亿", "state": "偏冷", "tone": "cyan", "score": 28},
            {"lo": 12000, "hi": 18000, "range": "12000 ~ 18000 亿", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 18000, "hi": 25000, "range": "18000 ~ 25000 亿", "state": "活跃 · 偏热", "tone": "orange", "score": 72},
            {"lo": 25000, "hi": 30000, "range": "25000 ~ 30000 亿", "state": "亢奋", "tone": "red", "score": 86},
            {"lo": 30000, "hi": None, "range": "> 30000 亿", "state": "极端", "tone": "red", "score": 96},
        ],
    },
    {
        "key": "turnover_rate",
        "group": GROUP_SENTIMENT,
        "name": "全A换手率",
        "subtitle": "两市成交额 ÷ 两市流通市值",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_SENTIMENT,
        "temp_weight": 0.30,
        "temp_invert": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "全市场换手率 = 两市股票成交金额 ÷ 两市股票流通市值。"
            "它把「成交额随市值自然变大」的因素剔除掉，是比成交额更干净的情绪度量。"
        ),
        "guide": (
            "方法论上用十年分位（不看绝对值，绝对水平会随市场结构漂移）。"
            "本页同时给出「读数区间档」与「十年分位」：分位样本不足 12 期时先按读数区间判读，"
            "历史积累到 12 期以上后以分位为准。经验区间：<1% 冰点 / 1~1.5% 偏冷 / "
            "1.5~2.5% 中性 / 2.5~4% 偏热 / >4% 亢奋。"
        ),
        "warning": "日频历史由本页逐日积累（回填窗口有限），分位窗口会在详情里标注实际样本量。",
        "bands": [
            {"lo": None, "hi": 1.0, "range": "< 1.0%", "state": "地量 · 冰点", "tone": "blue", "score": 10},
            {"lo": 1.0, "hi": 1.5, "range": "1.0 ~ 1.5%", "state": "偏冷", "tone": "cyan", "score": 30},
            {"lo": 1.5, "hi": 2.5, "range": "1.5 ~ 2.5%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 2.5, "hi": 4.0, "range": "2.5 ~ 4.0%", "state": "偏热", "tone": "orange", "score": 72},
            {"lo": 4.0, "hi": None, "range": "> 4.0%", "state": "亢奋", "tone": "red", "score": 90},
        ],
    },
    {
        "key": "margin",
        "group": GROUP_SENTIMENT,
        "name": "两融占流通市值",
        "subtitle": "融资余额 + 融券余额，占两市流通市值比例",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_SENTIMENT,
        "temp_weight": 0.20,
        "temp_invert": False,
        # 该指标打分的口径是「余额 ÷ 流通市值」，而历史序列存的是「余额」本身。
        # 余额有长期增长趋势，直接对它算分位会永远偏高，所以温度重建时跳过它，
        # 只用实时快照的读数（按占流通市值判档）参与温度。
        "temp_reconstruct": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "融资融券余额（杠杆资金规模）占两市流通市值的比重，衡量市场杠杆水位。"
            "余额由沪深两市账户信息合并口径取得，流通市值取交易所官方数据。"
        ),
        "guide": (
            ">3.5% 杠杆过热；2.5 ~ 3.5% 中性区间；<2.5% 杠杆低迷。"
            "看趋势比看点位更重要：连续回落 = 杠杆在退潮，行情持续性存疑。"
        ),
        "bands": [
            {"lo": None, "hi": 2.0, "range": "< 2.0%", "state": "杠杆低迷", "tone": "blue", "score": 15},
            {"lo": 2.0, "hi": 2.5, "range": "2.0 ~ 2.5%", "state": "偏低", "tone": "cyan", "score": 32},
            {"lo": 2.5, "hi": 3.0, "range": "2.5 ~ 3.0%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 3.0, "hi": 3.5, "range": "3.0 ~ 3.5%", "state": "偏高", "tone": "orange", "score": 70},
            {"lo": 3.5, "hi": None, "range": "> 3.5%", "state": "杠杆过热", "tone": "red", "score": 88},
        ],
    },
    {
        "key": "breadth",
        "group": GROUP_SENTIMENT,
        "name": "市场宽度",
        "subtitle": "上涨家数 ÷（上涨 + 下跌）",
        "unit": "%",
        "digits": 1,
        "temp_layer": None,
        "temp_weight": 0.0,
        "temp_invert": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "当日上涨家数占（上涨 + 下跌）家数的比例，反映赚钱效应是「普涨」还是「普跌」。"
            "指数可以靠权重股拉抬，宽度不会骗人。"
        ),
        "guide": ">80% 普涨（情绪亢奋）；40 ~ 60% 结构性；<30% 普跌（恐慌或缩量阴跌）。",
        "warning": "指数涨而宽度差 = 只有权重在涨，是典型的「赚指数不赚钱」，热度集中在少数板块。",
        "bands": [
            {"lo": 80, "hi": None, "range": "> 80%", "state": "普涨", "tone": "red", "score": 88},
            {"lo": 60, "hi": 80, "range": "60 ~ 80%", "state": "偏强", "tone": "orange", "score": 70},
            {"lo": 40, "hi": 60, "range": "40 ~ 60%", "state": "均衡 · 结构性", "tone": "lime", "score": 50},
            {"lo": 30, "hi": 40, "range": "30 ~ 40%", "state": "偏弱", "tone": "cyan", "score": 32},
            {"lo": None, "hi": 30, "range": "< 30%", "state": "普跌", "tone": "blue", "score": 12},
        ],
    },

    # ------------------------------------------------------------ ③ 资金层
    {
        "key": "fund_position",
        "group": GROUP_CAPITAL,
        "name": "公募仓位",
        "subtitle": "股票型基金仓位（周频，乐咕口径）",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_CAPITAL,
        "temp_weight": 0.30,
        "temp_invert": False,
        "freq": "周频",
        "judge": "percentile",
        "meaning": (
            "股票型基金的股票投资占净值比例（周频估算）。"
            "仓位越高说明公募可加仓的子弹越少、越拥挤，即所谓「仓位魔咒」（高仓 = 拥挤）。"
        ),
        "guide": (
            "该口径（股票型基金）长期仓位偏高（常年 90% 上下），所以看十年分位比看绝对水平更有意义："
            "分位 >80% 属高仓拥挤，<20% 属明显低仓、子弹充足。"
        ),
        "warning": "「88% 仓位魔咒」主要指偏股混合型；股票型基金因合同下限（≥80%）常年高位，请以十年分位为准。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "低仓 · 子弹充足", "tone": "blue", "score": 12},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏低", "tone": "cyan", "score": 30},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏高", "tone": "orange", "score": 70},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "高仓拥挤", "tone": "red", "score": 88},
        ],
    },
    {
        "key": "etf_share",
        "group": GROUP_CAPITAL,
        "name": "股票ETF份额",
        "subtitle": "宽基股票ETF 合计份额及其变化",
        "unit": "亿份",
        "digits": 0,
        "temp_layer": GROUP_CAPITAL,
        "temp_weight": 0.25,
        "temp_invert": False,
        "freq": "日频（快照）",
        "judge": "rate",
        "meaning": (
            "沪深300 / 中证500 / 中证1000 / 上证50 / 科创50 / 创业板指 / 中证A500 等"
            "宽基股票ETF 的合计份额。份额持续申购 = 机构（含国家队）在借道 ETF 进场，是最典型的逆势信号。"
        ),
        "guide": (
            "看份额的周变化方向：份额逆势增（指数跌、份额增）是最强的底部特征；"
            "份额持续赎回而指数在涨，说明反弹缺乏机构资金承接。"
        ),
        "warning": "份额为每日快照，变化需本页逐日积累后才可读；首次运行时只展示绝对水平。",
        "bands": [
            {"lo": 1.0, "hi": None, "range": "周变化 > +1.0%", "state": "持续申购 · 资金进场", "tone": "blue", "score": 15},
            {"lo": 0.2, "hi": 1.0, "range": "周变化 +0.2 ~ +1.0%", "state": "小幅净申购", "tone": "cyan", "score": 35},
            {"lo": -0.2, "hi": 0.2, "range": "周变化 −0.2 ~ +0.2%", "state": "持平", "tone": "lime", "score": 50},
            {"lo": -1.0, "hi": -0.2, "range": "周变化 −1.0 ~ −0.2%", "state": "小幅赎回", "tone": "orange", "score": 68},
            {"lo": None, "hi": -1.0, "range": "周变化 < −1.0%", "state": "持续赎回", "tone": "red", "score": 85},
        ],
    },
    {
        "key": "new_fund",
        "group": GROUP_CAPITAL,
        "name": "新基金发行",
        "subtitle": "近 30 日成立的权益类基金募集份额合计",
        "unit": "亿元",
        "digits": 0,
        "temp_layer": GROUP_CAPITAL,
        "temp_weight": 0.25,
        "temp_invert": False,
        "freq": "月频",
        "judge": "percentile",
        "meaning": (
            "近 30 日新成立基金中「股票型 + 混合型 + 指数型-股票」的募集份额合计（1 亿元 = 1 亿份）。"
            "新发是散户与渠道情绪的镜子：冰点（几十亿）出现在底部，千亿抢购往往出现在阶段性顶部。"
        ),
        "guide": (
            "看月度序列的分位：分位低 = 发行冰点（往往对应底部区域）；分位高 = 发行火爆（顶部信号）。"
            "同时看近 90 日/近一年的量级，避免单月波动误导。"
        ),
        "warning": "「千亿抢购」是顶部特征，不是利好——发行越火，后续增量资金越少。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "发行冰点 · 底部特征", "tone": "blue", "score": 12},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏冷", "tone": "cyan", "score": 30},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏热", "tone": "orange", "score": 70},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "千亿抢购 · 顶部信号", "tone": "red", "score": 90},
        ],
    },
    {
        "key": "industry_capital",
        "group": GROUP_CAPITAL,
        "name": "产业资本净增持",
        "subtitle": "近 30 日重要股东增减持净额",
        "unit": "亿元",
        "digits": 1,
        "temp_layer": GROUP_CAPITAL,
        "temp_weight": 0.20,
        "temp_invert": True,
        "freq": "月频",
        "judge": "value",
        "meaning": (
            "重要股东（高管、大股东、产业资本）二级市场及大宗交易增减持的净额（增持为正、减持为负），"
            "按变动数量 × 变动价格估算成金额。产业资本离公司基本面最近，被称作「最聪明的钱」。"
        ),
        "guide": (
            "净增持（尤其逆势增持潮）= 冷 / 底部特征；净减持放大（减持潮）= 热 / 需要警惕。"
            "看月度序列的「由负转正」转折，比看单月金额更有意义。"
        ),
        "bands": [
            {"lo": 100, "hi": None, "range": "净增持 > 100 亿", "state": "增持潮 · 底部特征", "tone": "blue", "score": 12},
            {"lo": 0, "hi": 100, "range": "净增持 0 ~ 100 亿", "state": "小幅净增持", "tone": "cyan", "score": 32},
            {"lo": -150, "hi": 0, "range": "净减持 0 ~ 150 亿", "state": "小幅净减持", "tone": "lime", "score": 52},
            {"lo": -400, "hi": -150, "range": "净减持 150 ~ 400 亿", "state": "净减持", "tone": "orange", "score": 72},
            {"lo": None, "hi": -400, "range": "净减持 > 400 亿", "state": "减持潮 · 偏热", "tone": "red", "score": 88},
        ],
    },

    # ------------------------------------------------------------ ④ 结构
    {
        "key": "structure_div",
        "group": GROUP_STRUCTURE,
        "name": "科创50 vs 创业板 · 分位差",
        "subtitle": "同期价格分位之差（pp），衡量热度是否在同一市场",
        "unit": "pp",
        "digits": 1,
        "temp_layer": None,
        "temp_weight": 0.0,
        "temp_invert": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "科创50 指数价格所处历史分位减去创业板指价格分位。差值为正说明资金集中在科创板（硬科技），"
            "为负说明热度在创业板（成长/新能源/医药等）。"
        ),
        "guide": (
            "分化超过 40pp 属极端分化：涨幅由单一市场驱动，属于结构性行情而不是全面牛。"
            "极端分化之后，往往出现方向切换（补涨或高位板块回撤）。"
        ),
        "warning": "这里用的是「价格分位」而非 PE 分位（科创50 上市时间较短，估值序列覆盖不足）。",
        "bands": [
            {"lo": 40, "hi": None, "range": "> +40pp", "state": "极端分化 · 单市场独热", "tone": "red", "score": 90},
            {"lo": 20, "hi": 40, "range": "+20 ~ +40pp", "state": "明显分化", "tone": "orange", "score": 70},
            {"lo": -20, "hi": 20, "range": "−20 ~ +20pp", "state": "相对均衡", "tone": "lime", "score": 50},
            {"lo": -40, "hi": -20, "range": "−40 ~ −20pp", "state": "反向分化", "tone": "cyan", "score": 30},
            {"lo": None, "hi": -40, "range": "< −40pp", "state": "极端反向 · 成长独热", "tone": "blue", "score": 10},
        ],
    },
]

INDICATOR_MAP = {ind["key"]: ind for ind in INDICATORS}
INDICATOR_KEYS = [ind["key"] for ind in INDICATORS]
AUTO_KEYS = INDICATOR_KEYS
AUTO_SOURCES = {"akshare", "fallback"}
TEMP_KEYS = [ind["key"] for ind in INDICATORS if ind.get("temp_weight")]

# 抓取失败且库中无记录时的兜底参考值（数据取自方法论原文 2026-09-18 的读数）。
# 仅用于「页面首次打开时不至于空白」，source 标记为 fallback，首次抓取成功即被覆盖。
FALLBACK_DEFAULTS: Dict[str, Any] = {
    "hs300_pe": {"value": 13.4, "as_of": "2026-09-18（兜底参考值）"},
    "hs300_pb": {"value": 1.40, "as_of": "2026-09-18（兜底参考值）"},
    "erp": {"value": 5.77, "as_of": "2026-09-18（兜底参考值）"},
    "below_na": {"value": 8.4, "as_of": "2026-09-18（兜底参考值）"},
    "turnover_amt": {"value": 20000, "as_of": "2026-09-18（兜底参考值）"},
    "margin": {"value": 2.69, "as_of": "2026-09-18（兜底参考值）"},
}


# ================================================================ 通用工具

def _num(v) -> Optional[float]:
    """安全转 float；None / NaN / Inf / 非数值一律返回 None。"""
    if v is None or v == "" or v == "-":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _d(v) -> Optional[str]:
    """安全转日期字符串 YYYY-MM-DD / YYYY-MM。"""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    return s[:10]


def _clean_series(rows: List[Tuple[Optional[str], Optional[float]]]) -> List[Dict[str, Any]]:
    """去掉空值、按日期去重（后出现的覆盖先出现的）、按日期正序。"""
    bag: Dict[str, float] = {}
    for as_of, value in rows:
        if not as_of or value is None:
            continue
        bag[as_of] = value
    return [{"as_of": k, "value": bag[k]} for k in sorted(bag)]


def percentile_rank(value: Optional[float], series: List[Dict[str, Any]],
                    freq: str = "M", invert: bool = False) -> Tuple[Optional[float], Optional[str]]:
    """value 在 series（正序历史序列）最近 N 个点中的分位（0-100）。

    返回 (分位, 分位窗口说明)。样本点少于 12 个时视为样本不足，返回 (None, 说明)。
    分位一律返回「原始分位」（高 = 数值高），是否反向由调用方按 temp_invert 处理。
    """
    vals = [x["value"] for x in series if x.get("value") is not None]
    if value is None or not vals:
        return None, None
    cap = PCT_WINDOW_POINTS.get(freq, 120)
    windowed = vals[-cap:] if len(vals) > cap else vals
    if len(windowed) < 12:
        label = "样本仅 %d 期，不足 12 期" % len(windowed)
        return None, label
    below = sum(1 for v in windowed if v <= value)
    pct = round(below / len(windowed) * 100, 1)
    if len(windowed) >= cap:
        label = FREQ_LABELS.get(freq, "近十年")
    else:
        label = "自 %s 起共 %d 期" % (series[-len(windowed)]["as_of"], len(windowed))
    return pct, label


def band_of(ind: Dict[str, Any], value: Optional[float]) -> Optional[Dict[str, Any]]:
    """判定 value 落在哪一档。"""
    if value is None:
        return None
    for b in ind.get("bands") or []:
        lo, hi = b.get("lo"), b.get("hi")
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return b
    return None


def dump_detail(detail: Optional[dict]) -> str:
    try:
        return json.dumps(detail or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def load_detail(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


# ================================================================ 各数据源抓取

def _ak():
    """惰性导入 akshare（启动时不必加载这个重包）。"""
    import akshare as ak
    return ak


def _legu_series(df, date_col: str, value_col: str) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append((_d(r.get(date_col)), _num(r.get(value_col))))
    return _clean_series(rows)


def fetch_hs300_pe() -> Dict[str, Any]:
    ak = _ak()
    df = ak.stock_index_pe_lg(symbol="沪深300")
    series = _legu_series(df, "日期", "滚动市盈率")
    if not series:
        raise RuntimeError("乐咕乐股未返回沪深300 PE 数据")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "M")
    static = _num(df["静态市盈率"].iloc[-1]) if "静态市盈率" in df.columns else None
    return {
        "value": round(last["value"], 2),
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "PE-TTM": round(last["value"], 2),
            "PE-静态": round(static, 2) if static is not None else None,
            "分位": pct,
            "分位窗口": pct_window,
            "样本期数": len(series),
            "数据源": "乐咕乐股 stock_index_pe_lg",
        },
        "series": series,
    }


def fetch_hs300_pb() -> Dict[str, Any]:
    ak = _ak()
    df = ak.stock_index_pb_lg(symbol="沪深300")
    series = _legu_series(df, "日期", "市净率")
    if not series:
        raise RuntimeError("乐咕乐股未返回沪深300 PB 数据")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "M")
    median = _num(df["市净率中位数"].iloc[-1]) if "市净率中位数" in df.columns else None
    return {
        "value": round(last["value"], 2),
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "PB": round(last["value"], 2),
            "PB中位数": round(median, 2) if median is not None else None,
            "分位": pct,
            "分位窗口": pct_window,
            "样本期数": len(series),
            "数据源": "乐咕乐股 stock_index_pb_lg",
        },
        "series": series,
    }


def fetch_y10_series() -> List[Dict[str, Any]]:
    """中国 10 年期国债收益率日频序列（2014 年起，覆盖十年分位所需窗口）。"""
    ak = _ak()
    start = (date.today() - timedelta(days=365 * 11)).strftime("%Y%m%d")
    df = ak.bond_zh_us_rate(start_date=start)
    rows = []
    for _, r in df.iterrows():
        rows.append((_d(r.get("日期")), _num(r.get("中国国债收益率10年"))))
    return _clean_series(rows)


def _month_end_map(daily: List[Dict[str, Any]]) -> Dict[str, float]:
    """把日频序列压成 {YYYY-MM: 当月最后一个可用值}。"""
    out: Dict[str, float] = {}
    for p in daily:
        if p.get("value") is None or not p.get("as_of"):
            continue
        out[p["as_of"][:7]] = p["value"]
    return out


def build_erp_series(pe_series: List[Dict[str, Any]],
                     y10_daily: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """ERP = 100 / PE-TTM − 10Y 国债收益率(%)，按月份对齐（PE 月频 → 取当月国债收益率末值）。"""
    y10_m = _month_end_map(y10_daily)
    rows = []
    for p in pe_series:
        pe = p.get("value")
        if not pe or pe <= 0:
            continue
        y = y10_m.get(p["as_of"][:7])
        if y is None:
            continue
        rows.append((p["as_of"], round(100.0 / pe - y, 2)))
    return _clean_series(rows)


def fetch_erp(pe_series: Optional[List[Dict[str, Any]]] = None,
              y10_daily: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if pe_series is None:
        pe_series = fetch_hs300_pe()["series"]
    if y10_daily is None:
        y10_daily = fetch_y10_series()
    series = build_erp_series(pe_series, y10_daily)
    if not series:
        raise RuntimeError("无法合成 ERP 序列（PE 与国债收益率无重叠月份）")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "M")
    pe_now = next((p["value"] for p in reversed(pe_series)), None)
    y_now = y10_daily[-1] if y10_daily else None
    return {
        "value": round(last["value"], 2),
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "ERP": round(last["value"], 2),
            "1/PE": round(100.0 / pe_now, 2) if pe_now else None,
            "10Y国债": round(y_now["value"], 3) if y_now else None,
            "国债日期": y_now["as_of"] if y_now else None,
            "分位": pct,
            "分位窗口": pct_window,
            "均值": round(mean([p["value"] for p in series[-120:]]), 2),
            "标准差": round(pstdev([p["value"] for p in series[-120:]]), 2),
            "数据源": "沪深300 PE-TTM ÷ 中债10年国债收益率",
        },
        "series": series,
    }


def fetch_below_na() -> Dict[str, Any]:
    ak = _ak()
    df = ak.stock_a_below_net_asset_statistics(symbol="全部A股")
    rows = []
    for _, r in df.iterrows():
        ratio = _num(r.get("below_net_asset_ratio"))
        rows.append((_d(r.get("date")), round(ratio * 100, 2) if ratio is not None else None))
    series = _clean_series(rows)
    if not series:
        raise RuntimeError("乐咕乐股未返回破净股统计")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "D")
    head = df.iloc[-1]
    return {
        "value": last["value"],
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "破净率": last["value"],
            "破净家数": int(_num(head.get("below_net_asset")) or 0),
            "公司总数": int(_num(head.get("total_company")) or 0),
            "分位": pct,
            "分位窗口": pct_window,
            "数据源": "乐咕乐股 stock_a_below_net_asset_statistics（全部A股）",
        },
        "series": series,
    }


# ------------------------------------------------------------ 交易所（成交额 / 流通市值）

def _compact(day: str) -> str:
    """YYYY-MM-DD → YYYYMMDD（深交所接口只接受紧凑格式，传带横线的日期会解析异常）。"""
    return str(day).replace("-", "")[:8]


def _sse_day(day: str) -> Dict[str, Optional[float]]:
    """上交所每日概况：股票口径的成交金额与流通市值（单位：亿元）。

    stock_sse_deal_daily 的「单日情况」行名是固定赋值，依次为：
    挂牌数 / 市价总值 / 流通市值 / 成交金额 / 成交量 / 平均市盈率 / 换手率 / 流通换手率。
    """
    ak = _ak()
    df = ak.stock_sse_deal_daily(date=_compact(day))
    if df is None or df.empty or "股票" not in df.columns or "单日情况" not in df.columns:
        return {}
    col = df.set_index("单日情况")["股票"]
    turnover = _num(col.get("成交金额"))
    float_mcap = _num(col.get("流通市值"))
    total_mcap = _num(col.get("市价总值"))
    listing = _num(col.get("挂牌数"))
    exchange_rate = _num(col.get("流通换手率"))
    return {
        "turnover": turnover,
        "float_mcap": float_mcap,
        "total_mcap": total_mcap,
        "listing": listing,
        "turnover_rate": exchange_rate,
    }


def _szse_day(day: str) -> Dict[str, Optional[float]]:
    """深交所市场总貌：股票口径的成交金额与流通市值（原始单位：元 → 亿元）。"""
    ak = _ak()
    df = ak.stock_szse_summary(date=_compact(day))
    if df is None or df.empty or "证券类别" not in df.columns:
        return {}
    hit = df[df["证券类别"] == "股票"]
    if hit.empty:
        return {}
    r = hit.iloc[0]
    turnover = _num(r.get("成交金额"))
    float_mcap = _num(r.get("流通市值"))
    total_mcap = _num(r.get("总市值"))

    def _yi(v):
        if v is None:
            return None
        # 深交所返回的是「元」，量级远大于亿元；做个量级保护，避免单位漂移时静默出错
        return round(v / 1e8, 2) if v > 1e7 else round(v, 2)

    return {
        "turnover": _yi(turnover),
        "float_mcap": _yi(float_mcap),
        "total_mcap": _yi(total_mcap),
    }


def trading_days_ago(n: int) -> List[str]:
    """最近 n 个交易日（含今天）的日期列表，正序。取不到交易日历时退化为工作日。"""
    days: List[str] = []
    try:
        ak = _ak()
        cal = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
        today = date.today().strftime("%Y-%m-%d")
        days = [str(v)[:10] for v in cal[col].tolist() if str(v)[:10] <= today]
    except Exception as e:  # 交易日历不可用时退化为工作日，不影响主流程
        logger.warning("获取交易日历失败，退化为一周工作日: %s", e)
    if not days:
        cur = date.today()
        while len(days) < n:
            if cur.weekday() < 5:
                days.append(cur.strftime("%Y-%m-%d"))
            cur -= timedelta(days=1)
        days.reverse()
    return days[-n:]


def _market_day(day: str) -> Optional[Dict[str, float]]:
    """取某一交易日的两市成交额与流通市值。任一侧缺失返回 None。"""
    sse = _sse_day(day)
    szse = _szse_day(day)
    if not sse or not szse:
        return None
    if sse.get("turnover") is None or szse.get("turnover") is None:
        return None
    turnover = round(sse["turnover"] + szse["turnover"], 2)
    fm = None
    if sse.get("float_mcap") is not None and szse.get("float_mcap") is not None:
        fm = round(sse["float_mcap"] + szse["float_mcap"], 2)
    return {
        "turnover": turnover,
        "sse_turnover": sse["turnover"],
        "szse_turnover": szse["turnover"],
        "float_mcap": fm,
        "sse_float_mcap": sse.get("float_mcap"),
        "szse_float_mcap": szse.get("float_mcap"),
        "sse_turnover_rate": sse.get("turnover_rate"),
    }


def fetch_market_series(days: int = 0) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, float]]]:
    """取最近 (1 + days) 个交易日的两市成交额 / 换手率 / 流通市值序列。

    返回 (成交额序列, 换手率序列, 流通市值序列, 最新一日明细)。
    days=0 时只取最新可用的一个交易日（刷新用），days>0 时回填这么多天（回填用）。
    """
    want = max(1, days) if days else 1
    # 多取一段候选日期，容忍节假日/数据未发布造成的空洞
    candidates = trading_days_ago(want + 12 if days else 12)
    if not days:
        candidates = candidates[-12:]

    turnover_series: List[Dict[str, Any]] = []
    rate_series: List[Dict[str, Any]] = []
    mcap_series: List[Dict[str, Any]] = []
    latest: Optional[Dict[str, float]] = None

    for day in reversed(candidates):  # 从最新往回走，凑够 want 天就停
        if len(turnover_series) >= want and days:
            break
        try:
            rec = _market_day(day)
        except Exception as e:
            logger.warning("交易所数据 %s 获取失败: %s", day, e)
            rec = None
        if not rec or rec.get("turnover") is None:
            continue
        turnover_series.append({"as_of": day, "value": rec["turnover"]})
        mcap_series.append({"as_of": day, "value": rec["float_mcap"]})
        if rec.get("float_mcap"):
            rate = round(rec["turnover"] / rec["float_mcap"] * 100, 3)
            rate_series.append({"as_of": day, "value": rate})
        if latest is None:
            latest = dict(rec, as_of=day)
            latest["turnover_rate"] = rate_series[-1]["value"] if rate_series else None
        if not days:
            break

    turnover_series = sorted(turnover_series, key=lambda x: x["as_of"])
    rate_series = sorted(rate_series, key=lambda x: x["as_of"])
    mcap_series = sorted(mcap_series, key=lambda x: x["as_of"])
    return turnover_series, rate_series, mcap_series, latest


# ------------------------------------------------------------ 两融

def fetch_margin(float_mcap_yi: Optional[float] = None,
                 days: int = 0) -> Dict[str, Any]:
    """两融余额（亿元）与占流通市值比例。"""
    ak = _ak()
    df = ak.stock_margin_account_info()
    rows = []
    for _, r in df.iterrows():
        rz = _num(r.get("融资余额"))
        rq = _num(r.get("融券余额"))
        if rz is None:
            continue
        rows.append((_d(r.get("日期")), round(rz + (rq or 0), 2)))
    series = _clean_series(rows)
    if not series:
        raise RuntimeError("未取到融资融券账户信息")
    last = series[-1]
    ratio = None
    if float_mcap_yi:
        ratio = round(last["value"] / float_mcap_yi * 100, 2)

    def _ago(n: int) -> Optional[float]:
        if len(series) <= n:
            return None
        return series[-1 - n]["value"]

    changes = {}
    for label, n in (("近5个交易日", 5), ("近20个交易日", 20), ("近60个交易日", 60)):
        prev = _ago(n)
        if prev:
            changes[label] = round((last["value"] - prev) / prev * 100, 2)
    # 连续回落周数（按 5 个交易日 = 1 周粗略折算）
    streak = 0
    for i in range(len(series) - 1, 0, -1):
        if series[i]["value"] < series[i - 1]["value"]:
            streak += 1
        else:
            break

    return {
        "value": ratio,
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "两融余额(万亿)": round(last["value"] / 10000, 4),
            "占流通市值%": ratio,
            "流通市值(万亿)": round(float_mcap_yi / 10000, 2) if float_mcap_yi else None,
            "近5日变化%": changes.get("近5个交易日"),
            "近20日变化%": changes.get("近20个交易日"),
            "近60日变化%": changes.get("近60个交易日"),
            "连续回落交易日": streak,
        },
        "series": series,
    }


# ------------------------------------------------------------ 市场宽度

def fetch_breadth() -> Dict[str, Any]:
    ak = _ak()
    df = ak.stock_market_activity_legu()
    kv = {}
    for _, r in df.iterrows():
        kv[str(r.get("item"))] = r.get("value")

    def _f(name):
        v = kv.get(name)
        if isinstance(v, str):
            v = v.replace("%", "")
        return _num(v)

    up, down = _f("上涨"), _f("下跌")
    if up is None or down is None or (up + down) <= 0:
        raise RuntimeError("赚钱效应接口未返回涨跌家数")
    rate = round(up / (up + down) * 100, 1)
    stat_date = str(kv.get("统计日期") or "")[:10]
    return {
        "value": rate,
        "as_of": stat_date or date.today().strftime("%Y-%m-%d"),
        "source": "akshare",
        "detail": {
            "上涨家数": int(up),
            "下跌家数": int(down),
            "涨停": int(_f("涨停") or 0),
            "跌停": int(_f("跌停") or 0),
            "平盘": int(_f("平盘") or 0),
            "活跃度": str(kv.get("活跃度") or ""),
            "统计时间": str(kv.get("统计日期") or ""),
            "数据源": "乐咕乐股 stock_market_activity_legu",
        },
        "series": None,
    }


# ------------------------------------------------------------ 公募仓位

def fetch_fund_position() -> Dict[str, Any]:
    ak = _ak()
    df = ak.fund_stock_position_lg()
    series = _legu_series(df, "date", "position")
    series = [p for p in series if p["value"] and p["value"] > 0]
    if not series:
        raise RuntimeError("乐咕乐股未返回基金仓位数据")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "W")
    detail = {
        "股票型基金仓位%": round(last["value"], 2),
        "分位": pct,
        "分位窗口": pct_window,
        "样本期数": len(series),
        "数据源": "乐咕乐股 fund_stock_position_lg",
    }
    # 灵活配置型仓位作为对照（数据源偶有停更，取不到就不展示）
    try:
        df2 = ak.fund_linghuo_position_lg()
        s2 = [p for p in _legu_series(df2, "date", "position") if p["value"] and p["value"] > 0]
        if s2:
            detail["灵活配置型仓位%"] = round(s2[-1]["value"], 2)
            detail["灵活配置型日期"] = s2[-1]["as_of"]
    except Exception as e:
        logger.info("灵活配置型基金仓位未取到（忽略）: %s", e)
    return {
        "value": round(last["value"], 2),
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": detail,
        "series": series,
    }


# ------------------------------------------------------------ 股票ETF份额

# 宽基股票ETF 的识别规则。
# 关键点：指数名后必须**紧跟 ETF**，否则会把「沪深300医药ETF」「中证500信息技术ETF」
# 这类行业主题产品混进来（它们份额动辄几百亿，会把「宽基 ETF 份额」的口径彻底带偏）。
# 同时排除场外联接基金（「沪深300ETF联接A」里也含「沪深300ETF」字样）。
BROAD_ETF_RE = re.compile(
    r"(沪深300|中证300|中证500|中证1000|中证2000|中证A500|中证A50|中证A100"
    r"|上证50|上证180|上证380|深证100|深证300|双创50|科创板50|科创50|科创100|科创200"
    r"|创业板50|创业板|北证50)ETF"
)
ETF_EXCLUDE = ("联接", "增强", "LOF", "货币")


def fetch_etf_share(prev_value: Optional[float] = None) -> Dict[str, Any]:
    """宽基股票ETF 合计份额（亿份）+ 与上一次快照相比的变化率。"""
    ak = _ak()
    df = ak.fund_scale_open_sina(symbol="股票型基金")
    if df is None or df.empty or "最近总份额" not in df.columns:
        raise RuntimeError("新浪基金规模接口未返回份额数据")

    picked = []
    for _, r in df.iterrows():
        name = str(r.get("基金简称") or "").replace(" ", "")
        if "ETF" not in name or any(k in name for k in ETF_EXCLUDE):
            continue
        if not BROAD_ETF_RE.search(name):
            continue
        shares = _num(r.get("最近总份额"))
        if not shares or shares <= 0:
            continue
        picked.append({"code": str(r.get("基金代码") or ""), "name": name, "shares": shares})

    if not picked:
        raise RuntimeError("未匹配到宽基股票ETF 样本")
    total = sum(p["shares"] for p in picked) / 1e8  # 亿份
    picked.sort(key=lambda x: x["shares"], reverse=True)
    update_date = _d(df.iloc[0].get("更新日期")) or date.today().strftime("%Y-%m-%d")

    rate = None
    if prev_value:
        rate = round((total - prev_value) / prev_value * 100, 2)

    return {
        "value": round(total, 1),
        "as_of": update_date,
        "source": "akshare",
        "detail": {
            "合计份额(亿份)": round(total, 1),
            "样本只数": len(picked),
            "较上次快照变化%": rate,
            "前三大": " | ".join(
                "%s %.1f亿份" % (p["name"][:16], p["shares"] / 1e8) for p in picked[:3]
            ),
            "数据源": "新浪 fund_scale_open_sina（宽基口径每日快照，变化需本页逐日积累）",
        },
        "series": None,
        "_prev_value": prev_value,
    }


# ------------------------------------------------------------ 新基金发行

EQUITY_TYPES = ("股票", "混合", "指数型-股票")


def _is_equity(fund_type: str) -> bool:
    t = str(fund_type or "")
    return any(k in t for k in EQUITY_TYPES)


def fetch_new_fund() -> Dict[str, Any]:
    """权益类新成立基金募集份额：近 30 / 90 / 365 日合计 + 月度序列（亿元）。"""
    ak = _ak()
    df = ak.fund_new_found_em()
    if df is None or df.empty:
        raise RuntimeError("东财新发基金接口未返回数据")

    monthly: Dict[str, float] = {}
    per_day: List[Tuple[str, float]] = []
    for _, r in df.iterrows():
        if not _is_equity(r.get("基金类型")):
            continue
        d = _d(r.get("成立日期"))
        amt = _num(r.get("募集份额"))
        if not d or amt is None or amt <= 0:
            continue
        per_day.append((d, amt))
        monthly[d[:7]] = round(monthly.get(d[:7], 0.0) + amt, 2)

    if not per_day:
        raise RuntimeError("新发基金中未解析到权益类募集份额")

    per_day.sort(key=lambda x: x[0])
    series = [{"as_of": k, "value": monthly[k]} for k in sorted(monthly)]

    today = date.today()
    windows = {}
    for label, nd in (("近30日", 30), ("近90日", 90), ("近365日", 365)):
        start = (today - timedelta(days=nd)).strftime("%Y-%m-%d")
        windows[label] = round(sum(a for d, a in per_day if d >= start), 2)

    value = windows["近30日"]
    # 用月度序列算「近 30 日」这一读数在历史月度分布中的位置：先按月均值折算
    monthly_avg = round(mean([p["value"] for p in series]), 2) if series else None
    pct, pct_window = percentile_rank(value, series, "M", ) if len(series) >= 12 else (None, None)

    return {
        "value": value,
        "as_of": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        "source": "akshare",
        "detail": {
            "近30日募集(亿)": windows["近30日"],
            "近90日募集(亿)": windows["近90日"],
            "近365日募集(亿)": windows["近365日"],
            "月度均值(亿)": monthly_avg,
            "分位": pct,
            "分位窗口": pct_window,
            "样本月数": len(series),
            "数据源": "东财 fund_new_found_em（仅统计股票型 / 混合型 / 指数型-股票）",
        },
        "series": series,
    }


# ------------------------------------------------------------ 产业资本净增持

EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _em_holdertrade(start: str, end: str, max_pages: int = 4) -> List[dict]:
    """东财数据中心 · 重要股东增减持明细（RPT_SHARE_HOLDER_INCREASE）。"""
    import requests

    out: List[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "reportName": "RPT_SHARE_HOLDER_INCREASE",
            "columns": "ALL",
            "filter": "(NOTICE_DATE>='%s')(NOTICE_DATE<='%s')" % (start, end),
            "pageNumber": str(page),
            "pageSize": "500",
            "sortColumns": "NOTICE_DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        }
        resp = requests.get(
            EM_DATACENTER, params=params,
            headers={"User-Agent": _EM_UA, "Referer": "https://data.eastmoney.com/"},
            timeout=30,
        )
        data = resp.json()
        result = data.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        out.extend(rows)
        if page >= int(result.get("pages") or 1):
            break
    return out


def _holdertrade_amount(rows: List[dict]) -> Tuple[float, float, int]:
    """把增减持明细折算成（净增持金额 亿元, 净变动股份 万股, 记录数）。"""
    amt = 0.0
    shares = 0.0
    used = 0
    for r in rows:
        n = _num(r.get("CHANGE_NUM_SYMBOL"))
        if n is None:
            n = _num(r.get("CHANGE_NUM"))
            direction = str(r.get("DIRECTION") or "")
            if direction == "减持" and n is not None:
                n = -abs(n)
        if n is None:
            continue
        price = _num(r.get("REAL_PRICE")) or _num(r.get("CLOSE_PRICE")) or _num(r.get("TRADE_AVERAGE_PRICE"))
        shares += n
        if price:
            # n 的单位是万股 → 金额（元）= n × 1e4 × price
            amt += n * 1e4 * price
        used += 1
    return round(amt / 1e8, 2), round(shares, 2), used


def _rolling30_window(months_ago: int) -> Tuple[str, str]:
    """返回「N 个月前的月末」往前 30 天的滚动窗口（含端点）。

    序列与当前读数统一用「近 30 日净额」口径，只是窗口终点不同（当月取到今天），
    这样卡片上的当前读数与图上的最后一根柱子永远对得上。
    """
    today = date.today()
    if months_ago <= 0:
        end = today
    else:
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        for _ in range(months_ago - 1):
            end = end.replace(day=1) - timedelta(days=1)
    start = end - timedelta(days=30)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def fetch_industry_capital(months: int = 1) -> Dict[str, Any]:
    """产业资本净增持：近 30 日净额（当前读数）+ 最近 N 个月的滚动 30 日序列（亿元）。"""
    series: List[Dict[str, Any]] = []
    used_last = 0
    for i in range(max(1, months)):
        s, e = _rolling30_window(i)
        try:
            rows = _em_holdertrade(s, e, max_pages=4)
        except Exception as ex:
            logger.warning("产业资本 %s 取数失败: %s", e[:7], ex)
            continue
        net, shares, used = _holdertrade_amount(rows)
        series.append({"as_of": e, "value": net, "shares": shares, "records": used})
        if i == 0:
            used_last = used
    series.sort(key=lambda x: x["as_of"])

    if not series:
        raise RuntimeError("未取到重要股东增减持数据")

    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "M") if len(series) >= 12 else (None, None)

    return {
        "value": last["value"],
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "近30日净增持(亿)": last["value"],
            "近30日净变动(万股)": last.get("shares"),
            "明细记录数": last.get("records") or used_last,
            "序列月数": len(series),
            "分位": pct,
            "分位窗口": pct_window,
            "数据源": "东财数据中心 RPT_SHARE_HOLDER_INCREASE（重要股东增减持）",
        },
        "series": [{"as_of": p["as_of"], "value": p["value"]} for p in series],
    }


# ------------------------------------------------------------ 结构分化

def _index_close_series(symbol: str) -> List[Dict[str, Any]]:
    ak = _ak()
    df = ak.stock_zh_index_daily(symbol=symbol)
    return _clean_series([(_d(r.get("date")), _num(r.get("close"))) for _, r in df.iterrows()])


def _month_end_pct_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把日频收盘序列压成「每个月末的价格分位」序列（用截至该点的历史分布）。"""
    if not series:
        return []
    last_of_month: Dict[str, Dict[str, Any]] = {}
    for p in series:
        last_of_month[p["as_of"][:7]] = p
    months = sorted(last_of_month)
    out = []
    for m in months:
        point = last_of_month[m]
        upto = [x["value"] for x in series if x["as_of"] <= point["as_of"]]
        windowed = upto[-PCT_WINDOW_POINTS["D"]:]
        if len(windowed) < 60:
            continue
        below = sum(1 for v in windowed if v <= point["value"])
        out.append({"as_of": point["as_of"], "value": round(below / len(windowed) * 100, 1)})
    return out


def fetch_structure() -> Dict[str, Any]:
    """科创50 与创业板指的价格分位及其差值（结构分化）。"""
    kc = _index_close_series("sh000688")   # 科创50
    cy = _index_close_series("sz399006")   # 创业板指
    if not kc or not cy:
        raise RuntimeError("指数日线未取到（科创50 / 创业板指）")

    kc_last, cy_last = kc[-1], cy[-1]
    kc_pct, kc_win = percentile_rank(kc_last["value"], kc, "D")
    cy_pct, cy_win = percentile_rank(cy_last["value"], cy, "D")
    spread = round(kc_pct - cy_pct, 1) if (kc_pct is not None and cy_pct is not None) else None

    # 月度分位差序列（画「分化走势」用）
    kc_m = {p["as_of"]: p["value"] for p in _month_end_pct_series(kc)}
    cy_m = {p["as_of"]: p["value"] for p in _month_end_pct_series(cy)}
    series = [
        {"as_of": d, "value": round(kc_m[d] - cy_m[d], 1)}
        for d in sorted(set(kc_m) & set(cy_m))
    ]

    return {
        "value": spread,
        "as_of": max(kc_last["as_of"], cy_last["as_of"]),
        "source": "akshare",
        "detail": {
            "科创50 分位%": kc_pct,
            "创业板 分位%": cy_pct,
            "分位差(pp)": spread,
            "科创50 收盘": round(kc_last["value"], 2),
            "创业板 收盘": round(cy_last["value"], 2),
            "科创50 窗口": kc_win,
            "创业板 窗口": cy_win,
            "数据源": "新浪指数日线 stock_zh_index_daily（价格分位口径）",
        },
        "series": series or None,
    }


# ================================================================ 抓取编排

def fetch_all(exchange_days: int = 0, industry_months: int = 1,
              prev: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """抓取全部指标。每个指标独立 try，互不影响。

    - exchange_days：交易所日频数据取多少天（0 = 只取最新一天，回填时传更大的值）
    - industry_months：产业资本月度序列取多少个月
    - prev：库中已有读数（用于 ETF 份额变化率等「与上次比」的指标）
    """
    prev = prev or {}
    out: Dict[str, Dict[str, Any]] = {}

    def _run(key: str, fn, *args, **kwargs):
        try:
            res = fn(*args, **kwargs)
            out[key] = res
        except Exception as e:
            logger.warning("股票指标 %s 抓取失败: %s", key, e, exc_info=False)
            out[key] = {"error": str(e)[:200]}

    # 估值层：PE / PB / ERP 共用 PE 序列与国债序列，只请求一次
    pe_res = None
    try:
        pe_res = fetch_hs300_pe()
        out["hs300_pe"] = pe_res
    except Exception as e:
        logger.warning("股票指标 hs300_pe 抓取失败: %s", e)
        out["hs300_pe"] = {"error": str(e)[:200]}
    _run("hs300_pb", fetch_hs300_pb)

    y10 = None
    try:
        y10 = fetch_y10_series()
    except Exception as e:
        logger.warning("10 年期国债收益率抓取失败: %s", e)
    try:
        out["erp"] = fetch_erp(pe_res["series"] if pe_res else None, y10)
    except Exception as e:
        logger.warning("股票指标 erp 抓取失败: %s", e)
        out["erp"] = {"error": str(e)[:200]}

    _run("below_na", fetch_below_na)

    # 情绪层：交易所口径一次取数，供成交额 / 换手率 / 两融占比共用
    latest_market = None
    try:
        t_series, r_series, m_series, latest_market = fetch_market_series(exchange_days)
        if t_series:
            t_last = t_series[-1]
            t_pct, t_win = percentile_rank(t_last["value"], t_series, "D") if len(t_series) >= 12 else (None, None)
            out["turnover_amt"] = {
                "value": t_last["value"],
                "as_of": t_last["as_of"],
                "source": "akshare",
                "detail": {
                    "两市成交额(亿)": t_last["value"],
                    "沪市(亿)": latest_market.get("sse_turnover") if latest_market else None,
                    "深市(亿)": latest_market.get("szse_turnover") if latest_market else None,
                    "流通市值(万亿)": round((latest_market or {}).get("float_mcap", 0) / 10000, 2)
                    if latest_market and latest_market.get("float_mcap") else None,
                    "分位": t_pct,
                    "分位窗口": t_win,
                    "数据源": "上交所《每日概况》+ 深交所《市场总貌》",
                },
                "series": t_series if exchange_days else None,
            }
        if r_series:
            r_last = r_series[-1]
            r_pct, r_win = percentile_rank(r_last["value"], r_series, "D")
            out["turnover_rate"] = {
                "value": r_last["value"],
                "as_of": r_last["as_of"],
                "source": "akshare",
                "detail": {
                    "全A换手率%": r_last["value"],
                    "分位": r_pct,
                    "分位窗口": r_win or "样本不足 12 个交易日",
                    "样本交易日": len(r_series),
                    "数据源": "两市成交额 ÷ 两市流通市值（交易所官方数据合成）",
                },
                "series": r_series if exchange_days else None,
            }
        if m_series:
            out["total_float_mcap"] = {
                "value": m_series[-1]["value"],
                "as_of": m_series[-1]["as_of"],
                "source": "akshare",
                "detail": {
                    "两市流通市值(亿)": m_series[-1]["value"],
                    "数据源": "上交所 / 深交所官网",
                },
                "series": m_series,
            }
    except Exception as e:
        logger.warning("股票指标 交易所日频数据抓取失败: %s", e)
        for k in ("turnover_amt", "turnover_rate"):
            out.setdefault(k, {"error": str(e)[:200]})

    fm = None
    if latest_market and latest_market.get("float_mcap"):
        fm = latest_market["float_mcap"]
    elif prev.get("total_float_mcap"):
        fm = prev["total_float_mcap"]["value"] if isinstance(prev.get("total_float_mcap"), dict) else prev["total_float_mcap"]
    _run("margin", fetch_margin, fm)
    _run("breadth", fetch_breadth)

    # 资金层
    _run("fund_position", fetch_fund_position)
    etf_prev = None
    if prev.get("etf_share"):
        etf_prev = prev["etf_share"]["value"] if isinstance(prev["etf_share"], dict) else prev["etf_share"]
    _run("etf_share", fetch_etf_share, etf_prev)
    _run("new_fund", fetch_new_fund)
    _run("industry_capital", fetch_industry_capital, industry_months)

    # 结构
    try:
        out["structure_div"] = fetch_structure()
    except Exception as e:
        logger.warning("股票指标 structure_div 抓取失败: %s", e)
        out["structure_div"] = {"error": str(e)[:200]}

    return out


# ================================================================ 响应组装

def _detail_pairs(detail: dict, skip=("数据源", "分位窗口", "科创50窗口", "创业板窗口")) -> List[str]:
    """把 detail 里适合展示的键值对格式化成「k v」列表。"""
    parts = []
    for k, v in (detail or {}).items():
        if k in skip or v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            if abs(v) >= 1000:
                text = "{:,.0f}".format(v)
            else:
                text = ("%g" % round(v, 3))
        else:
            text = str(v)
        parts.append("%s %s" % (k, text))
    return parts


def judge(ind: Dict[str, Any], value: Optional[float], pct: Optional[float],
          detail: dict) -> Tuple[Optional[Dict[str, Any]], Optional[float], str]:
    """返回 (命中的分档, 用于判定的数值, 判定依据说明)。

    ⚠️ judge="percentile" 且分位拿不到时**不能退回用读数去匹配**这些档：
    那些档的上下界是 0~100 的分位数，拿 2.03% 这样的读数去匹配会命中「分位 < 20%」
    从而判成「极冷」——读数与档位量纲不同，是典型的静默错误。此时直接不定档。
    """
    mode = ind.get("judge") or "value"
    if mode == "percentile":
        if pct is not None:
            return band_of(ind, pct), pct, "按十年分位判定"
        return None, None, "分位样本不足（需 ≥12 期历史），暂不定档"
    if mode == "rate":
        rate = detail.get("较上次快照变化%")
        if rate is None:
            return None, None, "份额变化需历史积累（首次快照）"
        return band_of(ind, rate), rate, "按份额周变化率判定"
    return band_of(ind, value), value, "按读数判定"


def score_from(ind: Dict[str, Any], value: Optional[float],
               pct: Optional[float]) -> Optional[float]:
    """把一个指标折算成 0-100 的「热度分」（越高越热）。

    优先用分位（方法论要求「全部用历史分位说话」）；分位样本不足时退回该指标
    读数所命中的档位分（band score）。两者都拿不到则返回 None，该指标不参与合成。
    """
    invert = bool(ind.get("temp_invert"))
    if pct is not None:
        return round(100 - pct if invert else pct, 1)
    band = band_of(ind, value)
    if band is None:
        return None
    s = float(band.get("score", 50))
    return round(100 - s if invert else s, 1)


def _score_of(ind: Dict[str, Any], value: Optional[float], pct: Optional[float],
              detail: dict) -> Optional[float]:
    """实时卡片用：按 judge 的口径判档，再折算热度分（ETF 份额用「周变化率」判档）。"""
    mode = ind.get("judge") or "value"
    if mode == "rate":
        band = band_of(ind, detail.get("较上次快照变化%"))
        return round(float(band.get("score", 50)), 1) if band else None
    return score_from(ind, value, pct)


def build_item(ind: Dict[str, Any], rec: Optional[dict]) -> Dict[str, Any]:
    rec = rec or {}
    detail = rec.get("detail") or {}
    value = rec.get("value")
    pct = detail.get("分位")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None

    band, judged, basis = judge(ind, value, pct, detail)
    tone = band.get("tone") if band else "slate"
    source = rec.get("source") or "akshare"

    bands = []
    for b in ind.get("bands") or []:
        active = band is not None and b is band
        bands.append({
            "range": b.get("range"),
            "state": b.get("state"),
            "note": b.get("note"),
            "tone": b.get("tone"),
            "active": active,
        })

    item = {
        "key": ind["key"],
        "group": ind["group"],
        "name": ind["name"],
        "subtitle": ind["subtitle"],
        "unit": ind.get("unit") or "",
        "digits": ind.get("digits", 1),
        "freq": ind.get("freq"),
        "value": value,
        "as_of": rec.get("as_of") or "—",
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "error": rec.get("error"),
        "missing": value is None,
        "percentile": pct,
        "percentile_window": detail.get("分位窗口"),
        "judge_value": judged,
        "judge_basis": basis,
        "state": band.get("state") if band else ("数据缺失" if value is None else "—"),
        "tone": tone,
        "bands": bands,
        "detail_text": " · ".join(_detail_pairs(detail)),
        "detail": detail,
        "meaning": ind.get("meaning"),
        "guide": ind.get("guide"),
        "warning": ind.get("warning"),
        "temp_layer": ind.get("temp_layer"),
        "temp_weight": ind.get("temp_weight") or 0.0,
        "temp_invert": bool(ind.get("temp_invert")),
        "heat_score": _score_of(ind, value, pct, detail),
    }
    return item


def build_thermometer(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """按「估值 ×50% + 情绪 ×30% + 资金 ×20%」合成 0-100 市场温度计。"""
    layer_out = []
    for layer in TEMP_LAYERS:
        members = [
            it for it in items.values()
            if it.get("temp_layer") == layer["key"] and (it.get("temp_weight") or 0) > 0
        ]
        wsum = sum(float(it["temp_weight"]) for it in members)
        got = [(it, float(it["temp_weight"]), it.get("heat_score")) for it in members]
        usable = [(it, w) for it, w, s in got if s is not None]
        usable_w = sum(w for _, w in usable)
        score = None
        if usable_w > 0:
            score = round(sum(it["heat_score"] * w for it, w in usable) / usable_w, 1)
        layer_out.append({
            "key": layer["key"],
            "name": layer["name"],
            "weight": layer["weight"],
            "tone": layer["tone"],
            "desc": layer["desc"],
            "score": score,
            "coverage": round(usable_w / wsum * 100, 0) if wsum else 0.0,
            "members": [
                {
                    "key": it["key"],
                    "name": it["name"],
                    "weight": w,
                    "score": s,
                    "percentile": it.get("percentile"),
                    "state": it.get("state"),
                    "tone": it.get("tone"),
                    "value": it.get("value"),
                    "unit": it.get("unit"),
                    "digits": it.get("digits"),
                }
                for it, w, s in got
            ],
        })

    usable_layers = [l for l in layer_out if l["score"] is not None]
    total_w = sum(l["weight"] for l in usable_layers)
    if total_w > 0:
        total = round(sum(l["score"] * l["weight"] for l in usable_layers) / total_w, 1)
    else:
        total = None

    zone = None
    if total is not None:
        for z in TEMP_ZONES:
            if z["lo"] <= total < z["hi"]:
                zone = z
                break

    # 三层分离度：差值越大说明「三层打架」，越要看背离
    scores = [l["score"] for l in layer_out if l["score"] is not None]
    spread = round(max(scores) - min(scores), 1) if len(scores) >= 2 else None

    return {
        "value": total,
        "state": zone["state"] if zone else "数据不足",
        "tone": zone["tone"] if zone else "slate",
        "zones": TEMP_ZONES,
        "layers": layer_out,
        "spread": spread,
        "formula": "综合温度 = 估值温度 ×50% + 情绪温度 ×30% + 资金温度 ×20%",
        "coverage": round(
            sum(l["score"] is not None for l in layer_out) / len(layer_out) * 100, 0
        ),
    }


def build_response(raw: Dict[str, dict]) -> dict:
    """把库中读数（raw）组装成页面需要的结构。"""
    items: Dict[str, Dict[str, Any]] = {}
    for ind in INDICATORS:
        items[ind["key"]] = build_item(ind, raw.get(ind["key"]))

    groups = []
    for gkey in (GROUP_VALUATION, GROUP_SENTIMENT, GROUP_CAPITAL, GROUP_STRUCTURE):
        members = [items[k] for k in INDICATOR_KEYS if INDICATOR_MAP[k]["group"] == gkey]
        if not members:
            continue
        groups.append({
            "key": gkey,
            "title": GROUP_TITLES[gkey],
            "subtitle": GROUP_SUBTITLES[gkey],
            "tone": GROUP_TONES[gkey],
            "items": members,
        })

    thermo = build_thermometer(items)

    # 温度计历史（用于趋势图）：由分位/热度分逐期重算，见 stock_temp_refresh
    return {
        "status": "ok",
        "tones": TONES,
        "groups": groups,
        "thermometer": thermo,
        "indicators": items,
    }


def heat_score_of(rec_value: Optional[float], detail: dict, ind: Dict[str, Any]) -> Optional[float]:
    """给历史序列用：按已落库的 detail（含分位）折算热度分。"""
    pct = detail.get("分位")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    return _score_of(ind, rec_value, pct, detail)


# ================================================================ 温度计历史重建

def _to_month_end(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把任意频率序列压成「每月最后一个可用值」，用于月度口径的温度重建。"""
    bag: Dict[str, Dict[str, Any]] = {}
    for p in series or []:
        if p.get("value") is None or not p.get("as_of"):
            continue
        bag[p["as_of"][:7]] = {"as_of": p["as_of"], "value": p["value"]}
    return [bag[k] for k in sorted(bag)]


def _rolling_pct_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对月度序列逐点算滚动分位：每点只用「当时可见」的最多 120 个月样本。

    分位是相对量，必须逐点重算；用今天的分布去评价十年前的点会让曲线形状失真。
    """
    pts = _to_month_end(series)
    if len(pts) < 12:
        return []
    vals = [p["value"] for p in pts]
    cap = PCT_WINDOW_POINTS["M"]
    out = []
    for i in range(len(pts)):
        windowed = vals[max(0, i + 1 - cap): i + 1]
        if len(windowed) < 12:
            continue
        below = sum(1 for v in windowed if v <= vals[i])
        out.append({"as_of": pts[i]["as_of"], "value": round(below / len(windowed) * 100, 1)})
    return out


def reconstruct_temperature(series_map: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """用历史表里的序列重建「月度温度」轨迹。

    每条序列先按月取样；分位能算出来的（PE / PB / ERP / 破净率 / 公募仓位 / 新基金 …
    有长历史）用逐点滚动分位打分，算不出来的（成交额 / 换手率等日频指标才刚开始积累）
    退回用该月读数命中的档位分打分。三层都有值的月份才给综合温度——缺层时不给，
    避免画出「成分一直在变」的假曲线。
    """
    values_cache: Dict[str, List[Dict[str, Any]]] = {}
    pct_cache: Dict[str, List[Dict[str, Any]]] = {}
    for ind in INDICATORS:
        if not ind.get("temp_weight") or ind.get("temp_reconstruct") is False:
            continue
        rows = _to_month_end(series_map.get(ind["key"]) or [])
        if not rows:
            continue
        values_cache[ind["key"]] = rows
        pct_rows = _rolling_pct_series(rows)
        if pct_rows:
            pct_cache[ind["key"]] = pct_rows

    if not values_cache:
        return {"monthly": [], "note": "历史序列尚未积累，无法重建温度轨迹"}

    months = sorted({p["as_of"][:7] for rows in values_cache.values() for p in rows})

    def _latest(cache: Dict[str, List[Dict[str, Any]]], key: str, month: str):
        rows = cache.get(key)
        if not rows:
            return None
        hit = None
        for p in rows:
            if p["as_of"][:7] <= month:
                hit = p
            else:
                break
        return hit

    monthly = []
    for month in months:
        layers: Dict[str, Optional[float]] = {}
        for layer in TEMP_LAYERS:
            members = [
                i for i in INDICATORS
                if i.get("temp_layer") == layer["key"] and i.get("temp_weight")
            ]
            acc, wacc = 0.0, 0.0
            for ind in members:
                key = ind["key"]
                vp = _latest(values_cache, key, month)
                if vp is None:
                    continue
                pp = _latest(pct_cache, key, month)
                score = score_from(ind, vp["value"], pp["value"] if pp else None)
                if score is None:
                    continue
                w = float(ind["temp_weight"])
                acc += score * w
                wacc += w
            layers[layer["key"]] = round(acc / wacc, 1) if wacc > 0 else None

        total = None
        if all(layers.get(l["key"]) is not None for l in TEMP_LAYERS):
            total = round(sum(layers[l["key"]] * l["weight"] for l in TEMP_LAYERS), 1)

        monthly.append({
            "as_of": month + "-01",
            "valuation": layers.get(GROUP_VALUATION),
            "sentiment": layers.get(GROUP_SENTIMENT),
            "capital": layers.get(GROUP_CAPITAL),
            "total": total,
        })

    return {
        "monthly": monthly,
        "note": (
            "分位为逐点滚动口径（每点只用当时可见的历史）；分位样本不足 12 期的指标退回按读数档位打分，"
            "并按该月可得数据标注覆盖。三层齐备才给综合温度：日频情绪指标的历史由本页逐日积累，"
            "所以综合温度线会从「三个层都有数据」的月份才开始出现。"
        ),
    }
