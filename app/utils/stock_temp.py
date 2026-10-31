"""股票指标（A 股冷热三层温度计）数据获取、状态判定与综合温度合成。

体系来自「A 股冷热要分三层看」的方法论：估值层（贵不贵）、情绪层（亢奋不亢奋）、
资金层（钱进不进），最后合成一个 0-100 的市场温度计，全部用历史分位说话。

三组指标（2026-10-26 移除「结构 · 科创50 vs 创业板分位差」）：
- 估值层（valuation）：沪深300 PE-TTM / 沪深300 PB / ERP 股债性价比 / 破净率
- 情绪层（sentiment）：两市成交额 / 全A换手率 / 融资余额占流通市值 / 市场宽度
- 资金层（capital）  ：股票ETF份额 / 新基金发行 / 产业资本净增持

数据获取方式（全部自动，来源统一记为 source="akshare"，实际站点见各指标 note）：

⚠️ 2026-10-26 换源：legulegu 在沙箱出口 IP 上会被风控（连续 6~7 次即 429，
akshare 报「未找到 _csrf 令牌」），估值层 4 个指标改用**当日快照**接口，
不再拉长历史 —— 分位由本页历史表逐日累积样本自算（`_fill_percentiles`）。

- 中证指数官网（csindex.com.cn，经 akshare，xls 直取）
      · stock_zh_index_value_csindex("000300")        → 沪深300 市盈率（滚动/静态）与股息率
- 腾讯证券全A快照（qt.gtimg.cn，经 akshare，一次请求出三个指标）
      · stock_zh_a_spot_tx                            → 全A 市净率 <1 占比（破净率）、
                                                        涨跌幅分布（市场宽度）、
                                                        沪深300 成分加权 PB
- 中证官网成分清单（000300cons.xls，直连 requests）
      · 300 只成分券代码                          → 供沪深300 PB 加权使用
- 乐咕乐股（legulegu，经 akshare）—— **仅作兜底**，主源失败时才走
      · stock_index_pe_lg / stock_index_pb_lg         → 沪深300 PE-TTM / PB
      · stock_a_below_net_asset_statistics            → 全部A股破净率
      · stock_market_activity_legu                    → 涨跌家数（市场宽度）
- 交易所官网（经 akshare，无反爬、按日可取，用于回填）
      · stock_sse_deal_daily / stock_szse_summary    → 沪/深 股票成交金额与流通市值
- 中债 / 新浪（经 akshare）
      · bond_zh_us_rate                              → 中国 10 年期国债收益率
- 东财数据中心（datacenter-web.eastmoney.com，直连）
      · 融资融券账户信息                              → 两融余额（stock_margin_account_info）
      · RPT_SHARE_HOLDER_INCREASE                    → 重要股东增减持（产业资本净增持）
- 新浪 / 东财（经 akshare）
      · fund_scale_open_sina                         → 宽基股票ETF 份额快照
      · fund_new_found_em                            → 新成立基金募集份额

任一项抓取失败时不做异常抛出：该指标返回 error，页面按「数据缺失」展示并保留上一次
成功读数，不影响其余指标与页面渲染。
"""
import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

from app.utils import greed_index as greed_index_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 分组与色调

GROUP_VALUATION = "valuation"
GROUP_SENTIMENT = "sentiment"
GROUP_CAPITAL = "capital"

GROUP_TITLES = {
    GROUP_VALUATION: "① 估值层 · 贵不贵",
    GROUP_SENTIMENT: "② 情绪层 · 亢奋还是低迷",
    GROUP_CAPITAL: "③ 资金层 · 钱进不进",
}

GROUP_SUBTITLES = {
    GROUP_VALUATION: "月频慢变量 · 决定能不能长期拿",
    GROUP_SENTIMENT: "日频快变量 · 决定短线热不热",
    GROUP_CAPITAL: "日频/月频 · 决定行情有没有持续性",
}

GROUP_TONES = {
    GROUP_VALUATION: "blue",
    GROUP_SENTIMENT: "amber",
    GROUP_CAPITAL: "green",
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
        "weight": 0.35,
        "tone": "blue",
        "desc": "PE 分位 40% + ERP 30% + 破净率 30%。慢变量，定方向。",
    },
    {
        "key": GROUP_SENTIMENT,
        "name": "情绪温度",
        "weight": 0.35,
        "tone": "amber",
        "desc": "成交额分位 50% + 换手率分位 30% + 融资余额占流通市值 20%。快变量，定情绪。",
    },
    {
        "key": GROUP_CAPITAL,
        "name": "资金温度",
        "weight": 0.30,
        "tone": "green",
        "desc": "股票ETF份额 36% + 新基金发行 36% + 产业资本 28%。资金变量，定底气。",
    },
]

# 综合温度 = 估值 ×35% + 情绪 ×35% + 资金 ×30%
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
    "new_fund", "industry_capital", "etf_share",
    "temperature", "greed_index",
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
            "本页同时给出「读数区间档」与「十年分位」：分位始终按**现有样本**计算（不设最小期数），"
            "样本越多越准，详情里会标注实际期数。经验区间：<1% 冰点 / 1~1.5% 偏冷 / "
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
        "name": "融资余额占流通市值",
        "subtitle": "融资余额（不含融券）占两市流通市值比例",
        "unit": "%",
        "digits": 2,
        "temp_layer": GROUP_SENTIMENT,
        "temp_weight": 0.20,
        "temp_invert": False,
        # 历史序列与快照同口径（都是「占流通市值%」），但**历史点极少**：交易所逐日流通市值
        # 只能取到最新一天，所以占比序列是从启用起逐日累积的。分位不设最小样本期数
        # （样本少时分位就粗），故该指标暂不参与历史温度重建。
        "temp_reconstruct": False,
        "freq": "日频",
        "judge": "value",
        "meaning": (
            "融资余额（杠杆资金规模，不含融券）占两市流通市值的比重，衡量市场杠杆水位。"
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
        "key": "etf_share",
        "group": GROUP_CAPITAL,
        "name": "股票ETF份额",
        "subtitle": "宽基股票ETF 合计份额及其变化",
        "unit": "亿份",
        "digits": 0,
        "temp_layer": GROUP_CAPITAL,
        "temp_weight": 0.36,
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
        "temp_weight": 0.36,
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
        "temp_weight": 0.28,
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

]

INDICATOR_MAP = {ind["key"]: ind for ind in INDICATORS}
INDICATOR_KEYS = [ind["key"] for ind in INDICATORS]
AUTO_KEYS = INDICATOR_KEYS
AUTO_SOURCES = {"akshare", "fallback"}
TEMP_KEYS = [ind["key"] for ind in INDICATORS if ind.get("temp_weight")]

# ---------------------------------------------------------------- 仅展示指标
#
# 这些指标**照常抓取、照常落库、照常补分位**，但：
#   · 不进 INDICATOR_KEYS / 三层温度计 / 温度速览表（所以不会被加权进综合温度）
#   · 只在「市场贪婪指数」区块里以**权重 0** 的单卡展示，用于横向对照
# 只看不算是它们的全部意义：多一个估值参照，不让指针变得不可解释。
#
# 用户 2026-10-31 要求加科创100 PE-TTM（展示样式对齐沪深300 PE-TTM）。
# ⚠️ 加新项时：key 同时登记到 app/utils/greed_index.py 的 GREED_DISPLAY，
#    页面才会渲染卡片；本表只负责抓取与读数判定（状态/档位色）。
DISPLAY_ONLY_INDICATORS: List[Dict[str, Any]] = [
    {
        "key": "kc100_pe",
        "group": GROUP_VALUATION,
        "name": "科创100 PE-TTM",
        "subtitle": "上证科创板100指数滚动市盈率（仅展示）",
        "unit": "倍",
        "digits": 2,
        "temp_layer": GROUP_VALUATION,
        "temp_weight": 0.0,     # 权重 0：不进综合温度计
        "temp_invert": False,
        "freq": "月频",
        "judge": "percentile",
        "meaning": (
            "上证科创板100 指数（000698）成分股的滚动市盈率（PE-TTM，市值加权）。"
            "科创100 是科创板里中等市值的成长股集合，估值弹性比沪深300 大得多，"
            "与沪深300 PE-TTM 并排看，能直观感受「成长股相对宽基贵多少」。"
        ),
        "guide": (
            "读数高不等于「该卖」——科创板利润基数小、成长期公司多，PE 天然偏高，"
            "所以它只用来对照幅度：科创100 PE ÷ 沪深300 PE 的倍数走高，"
            "说明资金在往成长股跑；倍数回落说明风险偏好在收缩。"
            "本指标不参与贪婪指数与综合温度计的任何计算。"
        ),
        "warning": "权重 0，只作估值参照；不要用它的高低单独判断市场贵贱。",
        "bands": [
            {"lo": None, "hi": 20, "range": "分位 < 20%", "state": "历史低位 · 便宜", "tone": "blue", "score": 10},
            {"lo": 20, "hi": 40, "range": "分位 20 ~ 40%", "state": "偏低", "tone": "cyan", "score": 30},
            {"lo": 40, "hi": 60, "range": "分位 40 ~ 60%", "state": "中性", "tone": "lime", "score": 50},
            {"lo": 60, "hi": 80, "range": "分位 60 ~ 80%", "state": "偏高", "tone": "orange", "score": 70},
            {"lo": 80, "hi": None, "range": "分位 > 80%", "state": "历史高位 · 贵", "tone": "red", "score": 90},
        ],
    },
]

# 登记进 MAP（供 _fill_percentiles / build_response 复用同一套读数判定），
# 但**不进 INDICATOR_KEYS** —— 分组、速览表、温度计都按 INDICATOR_KEYS 遍历。
INDICATOR_MAP.update({ind["key"]: ind for ind in DISPLAY_ONLY_INDICATORS})
DISPLAY_ONLY_KEYS = [ind["key"] for ind in DISPLAY_ONLY_INDICATORS]

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

    ⚠️ **不设最小样本门槛**（2026-10-26 用户要求）：样本只有几期也照算 ——
    样本少时分位粗、样本多了自然准，不需要人为卡「攒够 N 期才给分位」。

    唯一的下限是「至少 2 个样本」：**1 个样本算不出分位**（当前值跟它自己比，
    「≤ 自己」恒成立 → 分位恒为 100%），那是伪信号而非精度问题。窗口里只有
    1 个点的指标会返回 (None, None)，交给 `_fill_percentiles` 用历史表里
    累积的样本补算（历史表通常不止 1 期）。

    返回 (分位, 分位窗口说明)。分位一律返回「原始分位」（高 = 数值高），
    是否反向由调用方按 temp_invert 处理。

    ⚠️ 用**中位秩**而不是「≤ 当前值的个数」（2026-10-26 修正）：样本里常有大量
    重复值（PB 连续几天都是 1.37），若把「等于」全算作低于，当前值等于历史值时
    分位会被顶到 100%，1.37 这种中枢读数会被误判成「极热·贵」。改成
    (小于数 + 等于数/2) / 总数，全等时落在 50%，是统计上标准的百分位定义。
    """
    vals = [x["value"] for x in series if x.get("value") is not None]
    if value is None or len(vals) < 2:
        return None, None
    cap = PCT_WINDOW_POINTS.get(freq, 120)
    windowed = vals[-cap:] if len(vals) > cap else vals
    less = sum(1 for v in windowed if v < value - 1e-9)
    equal = sum(1 for v in windowed if abs(v - value) <= 1e-9)
    pct = round((less + equal / 2) / len(windowed) * 100, 1)
    if len(windowed) >= cap:
        label = FREQ_LABELS.get(freq, "近十年")
    else:
        start_as_of = next((x["as_of"] for x in series if x.get("value") is not None), None)
        label = "自 %s 起共 %d 期" % (start_as_of, len(windowed))
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


# ---------------------------------------------------------------- 线性插值打分
#
# 原实现里每档只有一个常数分（band.score），落在同一档内的所有读数得分完全相同——
# 成交额 12000 亿和 17999 亿都算50 分，档内没有任何分辨率。
# 这里把「相邻两档的**交界值 → 两档分数的中点**」定义为一组锚点，
# 当前读数落在哪两个锚点之间，就在两点之间做线性插值，档内也能分出高下。
#
# 例：破净率 6~10% 档 score=30、10%+ 档 score=12
#     → 锚点 (6, 41) 与 (10, 21)，读数 8.96 → 41 + (8.96-6)/(10-6)×(21-41) = 26.2
#
# 锚点只覆盖相邻档交界那一段，最外侧两档是开区间：低于首锚点取首档分、
# 高于末锚点取末档分（即"读数再低也就只能是这个分数"的饱和语义）。

def _ascending_bands(ind: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按读数升序返回分档。

    声明顺序不固定（破净率、产业资本等按「越热越前」倒序写），必须重排，
    且开区间档（lo 为 None）永远排在最前。
    """
    bands = list(ind.get("bands") or [])
    return sorted(
        bands,
        key=lambda b: (0, 0.0) if b.get("lo") is None else (1, float(b["lo"])),
    )


def interpolation_anchors(ind: Dict[str, Any]) -> List[Tuple[float, float]]:
    """返回 (读数, 分数) 锚点序列（按读数升序）。

    锚点定义在**相邻两档的交界读数**上，分数取两档分数的中点——
    这样插值曲线在交界处连续，且不改变任何一档原本的分数中枢。
    """
    bands = _ascending_bands(ind)
    pts: List[Tuple[float, float]] = []
    for lo_band, hi_band in zip(bands, bands[1:]):
        edge = lo_band.get("hi")
        if edge is None:
            continue
        pts.append((
            float(edge),
            (float(lo_band.get("score", 50)) + float(hi_band.get("score", 50))) / 2.0,
        ))
    return pts


def interpolate_score(ind: Dict[str, Any], value: Optional[float]
                      ) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """在相邻锚点之间线性插值得分，返回 (分数, 锚点说明)。

    锚点说明形如 {"mode": "linear"|"floor"|"ceil",
                 "lo": (x, y), "hi": (x, y), "t": 0~1}，
    供页面展示"当前读数卡在哪两个锚点之间、插值比例多少"。
    """
    bands = _ascending_bands(ind)
    if not bands or value is None:
        return None, None
    if len(bands) == 1:
        s = float(bands[0].get("score", 50))
        return s, {"mode": "single", "score": s}

    pts = interpolation_anchors(ind)
    x = float(value)
    first_score = float(bands[0].get("score", 50))
    last_score = float(bands[-1].get("score", 50))

    # 最外侧：开区间饱和，取该侧档位分
    if not pts or x <= pts[0][0]:
        return first_score, {"mode": "floor", "lo": (pts[0][0] if pts else None, first_score)}
    if x >= pts[-1][0]:
        return last_score, {"mode": "ceil", "hi": (pts[-1][0], last_score)}

    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if x1 <= x < x2:
            t = (x - x1) / (x2 - x1) if x2 != x1 else 0.0
            return y1 + t * (y2 - y1), {
                "mode": "linear", "lo": (x1, y1), "hi": (x2, y2), "t": round(t, 4),
            }
    return last_score, {"mode": "ceil", "hi": (pts[-1][0], last_score)}


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


# ---------------------------------------------------------------- 乐咕乐股限流
#
# 现象（2026-10-18 实测）：一次「刷新数据」里hs300_pe / hs300_pb / below_na /
# erp 会**依次**打 legulegu，而 akshare 的每个接口内部都要
# 先GET 一次页面取 _csrf 令牌（stock_index_pe_lg 就硬编码了
# `get_cookie_csrf(url=".../sz50-ttm-lyr")`）。连续 6~7 次后legulegu 开始返回
# **HTTP 429**，响应正文是「访问过于频繁 - 乐咕乐股」的拦截页 —— 里面当然没有
# _csrf，于是 akshare 抛出
#   「legulegu 页面中未找到 _csrf 令牌，上游可能已改版或拦截了本次请求」
# 这个报错把「被限流」说成了「上游改版」，排查方向完全被带偏（实测同一时刻
# 手动访问页面 csrf 正常返回，站点并未改版）。
#
# 对策：给所有 legulegu 调用套一层串行节流 + 429 退避重试。
#   - 同一线程内用锁串行化，避免并发叠加请求
#   - 每次调用前保证最小间隔。实测阈值：0.4s 连续 6~7 次即429，
#     3s 在「刚被限流过」的冷却期内仍会失败，**6s 实测 5/5 全通**，故取 6s。
#     一次完整刷新要打4 个 legulegu 接口，约 24s，可接受（页面本就允许异步抓取）。
#   - 遇 429 / 502-504 / JSON 解析失败（冷却期的典型表现）都退避后重试
#     退避 30s → 60s → 120s → 240s：实测 legulegu 的冷却窗口长达 1~2 分钟，
#     短退避会在冷却期内继续挨打，白白耗尽重试次数。
_LEGU_MIN_INTERVAL = 6.0
_LEGU_MAX_RETRY = 4
_LEGU_BACKOFF_BASE = 30.0
_legu_lock = threading.Lock()
_legu_last_call = [0.0]


def _is_legu_throttled(exc: Exception) -> bool:
    """判断异常是否为 legulegu 限流/风控所致（决定是否值得退避重试）。

    akshare 把限流包装成好几种形态，都要看：
      - DataParsingError「未找到 _csrf 令牌」：429 拦截页里没有该标签
      - JSONDecodeError「Expecting value: line 1 ...」：接口在冷却期返回了
        HTML/空响应，akshare 调r.json() 时炸在这里（实测沙箱上会与429 交替出现）
      - APIError / requests 异常里带 429
    另外冷却期内上游还会回 502/503/504（网关等不到后端），同样属于「等一会就好」。

    只把「明确是限流特征」的归入重试；真正的解析 bug（如字段改名）不在此列，
    免得掩盖问题。
    """
    text = f"{type(exc).__name__}: {exc}"
    if any(code in text for code in ("429", "502", "503", "504")):
        return True
    if "_csrf" in text or "访问过于频繁" in text:
        return True
    # akshare 内部 requests 的 JSON 解析失败 —— 冷却期内的典型表现
    if "Expecting value" in text or "JSONDecodeError" in text:
        return True
    return False


def _legu_call(fn, *args, **kwargs):
    """调用 legulegu 系接口：串行节流 + 限流退避重试。"""
    last_exc = None
    for attempt in range(_LEGU_MAX_RETRY):
        with _legu_lock:
            # 保证与上一次调用至少间隔 _LEGU_MIN_INTERVAL
            wait = _legu_last_call[0] + _LEGU_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                throttled = _is_legu_throttled(e)
            else:
                _legu_last_call[0] = time.monotonic()
                return result
            _legu_last_call[0] = time.monotonic()

        if not throttled or attempt == _LEGU_MAX_RETRY - 1:
            raise last_exc
        # 指数退避：20s → 40s → 80s，给上游限流窗口足够恢复
        backoff = _LEGU_BACKOFF_BASE * (2 ** attempt)
        logger.warning(
            "legulegu 触发限流（第 %d 次），%.0fs 后重试：%s",
            attempt + 1, backoff, last_exc,
        )
        time.sleep(backoff)
    raise last_exc


def _legu_series(df, date_col: str, value_col: str) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append((_d(r.get(date_col)), _num(r.get(value_col))))
    return _clean_series(rows)


# ------------------------------------------------------------ 当日快照源（2026-10-26 换源）
#
# 为什么换：乐咕乐股（legulegu）在沙箱出口 IP 上会被风控 —— 连续 6~7 次请求即 429，
# akshare 把它包装成「页面中未找到 _csrf 令牌」，导致 stock_temp 整轮抓取失败。
# 本地网络出口与沙箱不同，本地跑通不代表线上跑通。
#
# 用户口径（2026-10-26 明确）：**不需要历史数据，只要当日值**；分位由本页历史表
# 逐日累积的样本自算（见 stock_temp_refresh._fill_percentiles），不拉长历史。
#
# 于是把 4 个 legu 调用换成「当日快照」接口。**注意 2026-10-26 线上实测的二次修正**：
#   ① 韭圈儿 index_value_name_funddb —— **不存在**（akshare 最新 1.19.3 里也没有此函数），
#      原先按它设计的方案作废，改用中证指数官网 stock_zh_index_value_csindex（0.2 秒）
#   ② 东财 stock_zh_a_spot_em —— 沙箱出口 RemoteDisconnected，改用腾讯
#      stock_zh_a_spot_tx（5577 行 / 5 秒，含 pn 市净率、zdf 涨跌幅、zsz 总市值）
#   ③ 沪深300 PB 没有现成当日源，用「中证成分清单 + 腾讯个股 pn 按总市值加权」现算
# 三个源都不需 cookie，也没有请求次数限制。legu 仅保留为**兜底**。

_SNAPSHOT_TTL = 120  # 秒：同一轮刷新里 PE/PB/破净率/宽度复用同一次请求
_snapshot_cache: Dict[str, Any] = {}


def _cached_snapshot(name: str, loader):
    """轮内缓存：一次刷新里多个指标共用同一份快照，避免重复打同一接口。"""
    hit = _snapshot_cache.get(name)
    now = time.time()
    if hit and now - hit[0] < _SNAPSHOT_TTL:
        return hit[1]
    data = loader()
    _snapshot_cache[name] = (now, data)
    return data


def _tx_snapshot():
    """腾讯证券全 A 实时快照（约 5577 行）。

    字段：code（带交易所前缀）/ name / pn（市净率）/ pe_ttm / zdf（涨跌幅%）/
    hsl（换手率）/ zsz（总市值）/ ltsz（流通市值）/ stock_type。

    2026-10-26：东财 stock_zh_a_spot_em 在沙箱出口直接 RemoteDisconnected，
    改用腾讯同口径快照。一次请求同时供「破净率 / 市场宽度 / 沪深300 加权 PB」三处使用。
    """
    ak = _ak()
    return _cached_snapshot("tx_all", lambda: ak.stock_zh_a_spot_tx())


def _tx_frame():
    df = _tx_snapshot()
    if df is None or len(df) == 0:
        raise RuntimeError("腾讯全A快照为空")
    return df


def _csindex_pe(symbol: str = "000300", cache_key: str = "csindex300") -> Dict[str, Any]:
    """中证指数官网估值（xls 直取，取最新一个交易日）。

    只给当日一行用：市盈率1 = 总市值/最近年报净利润（静态），
    市盈率2 = 总市值/最近四个季度净利润（滚动 TTM），本模块主值取**滚动**。

    symbol 默认 000300（沪深300）；科创100 用 000698（见 fetch_kc100_pe）。
    两者同一个接口、同一套字段，只是指数代码不同 —— 所以复用同一个函数，
    免得两处解析逻辑各写一遍、日后字段变了只改一处。
    """
    ak = _ak()
    df = _cached_snapshot(cache_key, lambda: ak.stock_zh_index_value_csindex(symbol=symbol))
    if df is None or len(df) == 0:
        raise RuntimeError("中证估值表为空")
    best = None
    for _, r in df.iterrows():
        d = _d(r.get("日期"))
        if not d:
            continue
        row = (d, _num(r.get("市盈率2")), _num(r.get("市盈率1")), _num(r.get("股息率1")))
        if best is None or row[0] > best[0]:
            best = row
    if best is None or not best[1]:
        raise RuntimeError("中证估值表未解析出市盈率")
    return {"as_of": best[0], "pe_ttm": best[1], "pe_static": best[2], "dividend": best[3]}


# 沪深300 成分券清单（中证官网 xls，当日成分 300 只，约 67KB / 0.3 秒）
_HS300_CONS_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/cons/000300cons.xls"
)


def _load_hs300_cons() -> Dict[str, str]:
    """下载并解析成分清单 → {腾讯代码: 成分券名称}，如 {'sh600519': '贵州茅台'}。

    中证文件里「成份券代码」是纯数字（1 = 平安银行），必须靠「交易所」列补前缀，
    不能按首位数字猜 —— 000001 与 600xxx 的规则虽可推，但北交所/新老代码会出错。
    """
    import io

    import pandas as pd
    import requests

    r = requests.get(_HS300_CONS_URL, timeout=20)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content))
    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        raw = row.get("成份券代码Constituent Code")
        if raw is None:
            continue
        digit = str(raw).strip().split(".")[0].zfill(6)
        if not digit.isdigit():
            continue
        ex = str(row.get("交易所Exchange") or "")
        prefix = "sh" if "上海" in ex else ("sz" if "深圳" in ex else "bj")
        out[prefix + digit] = str(row.get("成份券名称Constituent Name") or "").strip()
    if len(out) < 250:
        raise RuntimeError(f"沪深300 成分清单解析异常，仅 {len(out)} 只")
    return out


def _hs300_pb_weighted() -> Dict[str, Any]:
    """沪深300 PB ≈ Σ总市值 / Σ(总市值 / 个股PB)，用中证成分 + 腾讯快照现算。

    指数 PB 的定义就是「成分股总市值 ÷ 成分股净资产合计」，逐只个股按 1/PB 折出
    净资产再汇总即可，不需要任何历史序列。口径与中证官方（自由流通加权）有差异，
    但每天用同一套算法，**时间序列自洽**，分位不受影响。
    """
    cons = _cached_snapshot("hs300cons", _load_hs300_cons)
    df = _tx_frame()
    if "code" not in df.columns or "pn" not in df.columns:
        raise RuntimeError(f"腾讯快照字段异常: {list(df.columns)[:10]}")
    idx = {}
    for _, r in df.iterrows():
        idx[str(r.get("code") or "").strip().lower()] = r
    mv = nav = 0.0
    hit = 0
    for code in cons:
        row = idx.get(code)
        if row is None:
            continue
        pb = _num(row.get("pn"))
        mv_i = _num(row.get("zsz"))
        if not pb or pb <= 0 or not mv_i or mv_i <= 0:
            continue
        mv += mv_i
        nav += mv_i / pb
        hit += 1
    if hit < 200 or nav <= 0:
        raise RuntimeError(f"沪深300 加权 PB 可用成分过少（{hit}/{len(cons)}）")
    return {"pb": mv / nav, "hit": hit, "total": len(cons)}


def _fetch_hs300_pe_legu() -> Dict[str, Any]:
    """兜底：乐咕乐股 PE（带限流退避）。仅在韭圈儿快照不可用时调用。"""
    ak = _ak()
    df = _legu_call(ak.stock_index_pe_lg, symbol="沪深300")
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
            "数据源": "乐咕乐股 stock_index_pe_lg（兜底）",
        },
        "series": series,
    }


def fetch_hs300_pe() -> Dict[str, Any]:
    """沪深300 PE-TTM：优先中证指数官网当日估值，失败回退乐咕乐股。

    只返回当日一个点（series 为空）—— 分位由历史表累积样本补算，不拉长历史。
    """
    try:
        snap = _csindex_pe()
        return {
            "value": round(snap["pe_ttm"], 2),
            "as_of": snap.get("as_of"),
            "source": "akshare",
            "detail": {
                "PE-TTM": round(snap["pe_ttm"], 2),
                "PE-静态": round(snap["pe_static"], 2) if snap.get("pe_static") else None,
                "股息率": round(snap["dividend"], 2) if snap.get("dividend") else None,
                "分位": None,       # 交给 _fill_percentiles 用本页历史样本补算
                "数据源": "中证指数官网 stock_zh_index_value_csindex（000300 当日估值，市盈率2=滚动）",
            },
            "series": [],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("中证 PE 失败，回退乐咕乐股：%s", e)
        return _fetch_hs300_pe_legu()


def fetch_kc100_pe() -> Dict[str, Any]:
    """科创100 PE-TTM（中证 000698）：**只用于展示，不参与任何计算**。

    用户 2026-10-31 要求：在股市热度页「市场贪婪指数」的分项说明里，按沪深300
    PE-TTM 同样的样式再列一个科创100 PE-TTM，但只作对照展示 ——
    既不加进贪婪指数的权重（仍是 4×22% + 12%），也不进综合温度计的三层合成。

    所以它刻意**不在 INDICATORS 里**（那里是温度计成分表），而是登记在
    DISPLAY_ONLY_INDICATORS；抓取/落库/分位补算与其它指标走同一条链路，
    只是不参与加权。
    """
    snap = _csindex_pe(symbol="000698", cache_key="csindex698")
    return {
        "value": round(snap["pe_ttm"], 2),
        "as_of": snap.get("as_of"),
        "source": "akshare",
        "detail": {
            "PE-TTM": round(snap["pe_ttm"], 2),
            "PE-静态": round(snap["pe_static"], 2) if snap.get("pe_static") else None,
            "股息率": round(snap["dividend"], 2) if snap.get("dividend") else None,
            "分位": None,       # 交给 _fill_percentiles 用本页历史样本补算（仅供展示参考）
            "数据源": "中证指数官网 stock_zh_index_value_csindex（000698 当日估值，市盈率2=滚动）",
        },
        "series": [],
    }


def _fetch_hs300_pb_legu() -> Dict[str, Any]:
    """兜底：乐咕乐股 PB（带限流退避）。"""
    ak = _ak()
    df = _legu_call(ak.stock_index_pb_lg, symbol="沪深300")
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
            "数据源": "乐咕乐股 stock_index_pb_lg（兜底）",
        },
        "series": series,
    }


def fetch_hs300_pb() -> Dict[str, Any]:
    """沪深300 PB：用中证成分 + 腾讯快照加权现算（当日值），失败回退乐咕乐股。"""
    try:
        w = _hs300_pb_weighted()
        return {
            "value": round(w["pb"], 2),
            "as_of": date.today().strftime("%Y-%m-%d"),
            "source": "akshare",
            "detail": {
                "PB": round(w["pb"], 2),
                "成分覆盖": f"{w['hit']}/{w['total']}",
                "分位": None,
                "数据源": "中证成分 000300cons + 腾讯全A快照（个股市净率按总市值加权）",
            },
            "series": [],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("加权 PB 失败，回退乐咕乐股：%s", e)
        return _fetch_hs300_pb_legu()


def fetch_y10_series(days: int = 40) -> List[Dict[str, Any]]:
    """中国 10 年期国债收益率日频序列（**默认只取最近 40 天**）。

    2026-10-26 调整：原先拉 11 年（覆盖「十年分位」窗口）。现在 ERP 走当日口径
    （100/PE − 当日 10Y），分位由历史表累积样本补算，长序列在这里不再有用 ——
    拉 11 年纯粹是白下载 2700 多行。40 天足够容忍长假 + 取到最新一个发布日。

    仅在需要重建长序列（回填）时传更大的 days。
    """
    ak = _ak()
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
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
              y10_daily: Optional[List[Dict[str, Any]]] = None,
              pe_now: Optional[float] = None,
              pe_as_of: Optional[str] = None) -> Dict[str, Any]:
    """ERP = 100/PE-TTM − 10Y 国债收益率（%）。

    两条路径：
    ① 传了长 PE 序列 → 原逻辑，按月对齐合成整条 ERP 序列（回填/兜底用）；
    ② 只有当日 PE（2026-10-26 起的默认路径）→ 直接用当日 PE 与最新 10Y 合成**一个点**，
       分位交给 _fill_percentiles 用本页历史样本补算，不再拉长历史。
    """
    if pe_series is None and pe_now is None:
        res = fetch_hs300_pe()
        pe_series = res.get("series") or []
        pe_now = res.get("value")
        pe_as_of = res.get("as_of")
    if y10_daily is None:
        y10_daily = fetch_y10_series()

    series = build_erp_series(pe_series or [], y10_daily)
    if not series and pe_now:
        y10 = y10_daily[-1] if y10_daily else None
        if not y10 or y10.get("value") is None:
            raise RuntimeError("10 年期国债收益率不可用，无法合成 ERP")
        series = [{
            "as_of": pe_as_of or y10["as_of"],
            "value": round(100.0 / pe_now - y10["value"], 2),
        }]
    if not series:
        raise RuntimeError("无法合成 ERP 序列（PE 与国债收益率无重叠月份）")
    last = series[-1]
    pct, pct_window = percentile_rank(last["value"], series, "M")
    pe_now = pe_now or next((p["value"] for p in reversed(pe_series or [])), None)
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


def _fetch_below_na_legu() -> Dict[str, Any]:
    """兜底：乐咕乐股破净统计（带限流退避）。"""
    ak = _ak()
    df = _legu_call(ak.stock_a_below_net_asset_statistics, symbol="全部A股")
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
            "数据源": "乐咕乐股 stock_a_below_net_asset_statistics（全部A股·兜底）",
        },
        "series": series,
    }


def fetch_below_na() -> Dict[str, Any]:
    """破净率：用腾讯全 A 快照自算（市净率 < 1 的家数占比），失败回退乐咕乐股。

    该指标 judge=value，靠绝对档位打分，不依赖历史分位 —— 换源不影响判档。
    """
    try:
        df = _tx_frame()
        if "pn" not in df.columns:
            raise RuntimeError(f"腾讯快照无 pn（市净率）列: {list(df.columns)[:10]}")
        pb = [x for x in (_num(v) for v in df["pn"]) if x is not None and x > 0]
        if len(pb) < 1000:
            raise RuntimeError(f"腾讯快照有效市净率样本过少（{len(pb)}）")
        below = sum(1 for x in pb if x < 1)
        rate = round(below / len(pb) * 100, 2)
        return {
            "value": rate,
            "as_of": date.today().strftime("%Y-%m-%d"),
            "source": "akshare",
            "detail": {
                "破净率": rate,
                "破净家数": below,
                "样本家数": len(pb),
                "分位": None,
                "数据源": "腾讯 stock_zh_a_spot_tx（全A快照自算）",
            },
            "series": [],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯快照算破净率失败，回退乐咕乐股：%s", e)
        return _fetch_below_na_legu()


# ------------------------------------------------------------ 交易所（成交额 / 流通市值）

def _compact(day: str) -> str:
    """YYYY-MM-DD → YYYYMMDD（深交所接口只接受紧凑格式，传带横线的日期会解析异常）。"""
    return str(day).replace("-", "")[:8]


# ------------------------------------------------------------ 批量成交额（长历史）
#
# 交易所官方接口（stock_sse_deal_daily / stock_szse_summary）**一次只能取一天**，
# 2014 至今约 3100 个交易日 × 2 个接口 ≈ 6200 次请求，逐日抓不可行。
#
# 替代口径：用新浪指数日线的**成交量**合成两市成交额 —— 上证综指（sh000001）与
# 深证综指（sz399106）的成交量分别等于沪市 / 深市全部股票的成交量（指数日线 volume
# 字段单位是「股」，与交易所「成交量（亿股）」口径比对，实测比值稳定在 0.99 附近）。
#
# 成交额 = 成交量 × 均价。均价用「成交额 ÷ 成交量」的真实比值作为锚点，按指数点位
# 比例逐日折算：均价_t = 均价_锚点 × (点位_t / 点位_锚点)。锚点由 _sse_day() 取到的
# 交易所真实成交额与成交量现算，保证锚点当天与官方口径完全一致。
#
# ⚠️ 这是**近似口径**，与交易所官方逐日抓取存在约 1~5% 的浮动误差（源于均价折算而非
# 真实逐日均价）。适合长历史趋势判断；判断当前绝对档位时仍以官方逐日值为准。

_MARKET_HISTORY_INDEXES = (
    ("sh000001", "上证综指"),   # 沪市全部股票
    ("sz399106", "深证综指"),   # 深市全部股票
)


def _index_daily_volume(symbol: str) -> List[Dict[str, Any]]:
    """新浪指数日线 → [{as_of, close, volume}]（volume 单位：股）。"""
    ak = _ak()
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        d = str(r.get("date"))[:10]
        v = _num(r.get("volume"))
        if not d or v is None:
            continue
        out.append({"as_of": d, "close": _num(r.get("close")), "volume": v})
    return out


def fetch_market_series_bulk(
    start: str = "2014-01-01", anchor_day: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, float]], List[Dict[str, Any]]]:
    """用指数成交量批量合成两市成交额长历史。

    返回 (成交额序列, 换手率序列, 锚点明细, 近似流通市值序列)。
    与 fetch_market_series() 的区别：
    一次调用即可拿到 start 以来的全部交易日，不做逐日请求。

    换手率 = 成交额 ÷ 两市流通市值。流通市值同样没有批量历史源，这里用**指数点位
    同比缩放**的流通市值近似：锚点取交易所真实流通市值，之后按大盘指数点位比例折算，
    与「成交额 ÷ 流通市值」的真实口径保持同量纲（约 1~2% 误差，实测锚点日偏差 0.01%）。

    第四个返回值（近似流通市值逐日序列）：融资余额本身有官方长历史，
    （akshare 返回 2012 年以来 3400+ 期），配上逐日流通市值即可得到占比长序列。

    anchor_day 指定锚点交易日（默认最近一个有官方数据的交易日）；取不到官方数据时
    返回空序列并抛错，避免产出无锚点的假历史。
    """
    sh = {p["as_of"]: p for p in _index_daily_volume("sh000001")}
    sz = {p["as_of"]: p for p in _index_daily_volume("sz399106")}
    if not sh or not sz:
        raise RuntimeError("指数日线成交量获取失败，无法合成两市成交额")

    days = sorted(d for d in (set(sh) & set(sz)) if start <= d <= date.today().isoformat())
    if not days:
        raise RuntimeError("指数日线与起始日期 %s 无交集" % start)

    # ---- 锚点：用交易所真实值定标，保证锚点当天与官方口径一致
    anchor = None
    candidates = [d for d in reversed(days) if d == anchor_day] if anchor_day else \
        [d for d in reversed(days[-15:])]
    for d in candidates:
        try:
            rec = _market_day(d)
        except Exception:
            rec = None
        if rec and rec.get("turnover") and rec.get("float_mcap"):
            anchor = dict(rec)
            anchor["as_of"] = d
            break
    if not anchor:
        raise RuntimeError(
            "未能取得交易所锚点数据（最近 %s 均失败），拒绝产出无锚点的近似历史" % days[-3:]
        )

    # 锚点当日的指数合计成交量（股）→ 亿股，与交易所「成交量（亿股）」同量纲
    a_vol_yi = (sh[anchor["as_of"]]["volume"] + sz[anchor["as_of"]]["volume"]) / 1e8
    if a_vol_yi <= 0:
        raise RuntimeError("锚点日 %s 指数成交量异常" % anchor["as_of"])

    # 均价(元/股) = 成交额(亿) / 成交量(亿股)；流通市值(亿) 已知
    avg_price = anchor["turnover"] / a_vol_yi
    # 锚点日的大盘指数点位（沪+深 之和，只用作比例因子，不作绝对含义）
    a_point = (sh[anchor["as_of"]]["close"] or 0) + (sz[anchor["as_of"]]["close"] or 0)
    if a_point <= 0:
        raise RuntimeError("锚点日 %s 指数点位异常" % anchor["as_of"])

    turnover_series: List[Dict[str, Any]] = []
    rate_series: List[Dict[str, Any]] = []
    mcap_series: List[Dict[str, Any]] = []
    for d in days:
        s = sh[d]
        z = sz[d]
        vol_yi = (s["volume"] + z["volume"]) / 1e8
        if vol_yi <= 0:
            continue
        point = (s["close"] or 0) + (z["close"] or 0)
        if point <= 0:
            continue
        # 均价按点位比例缩放；成交额 = 成交量(亿股) × 均价(元/股) = 亿元
        est_price = avg_price * (point / a_point)
        turnover = round(vol_yi * est_price, 2)
        turnover_series.append({"as_of": d, "value": turnover})
        # 流通市值按点位比例缩放，与成交额保持同量纲
        est_mcap = anchor["float_mcap"] * (point / a_point)
        if est_mcap:
            rate_series.append({
                "as_of": d, "value": round(turnover / est_mcap * 100, 3)
            })
            # 逐日近似流通市值：融资余额有官方长历史，但逐日流通市值原本没有
            # 批量源，这里顺带产出（供 long_history 备查用，不参与分位计算）
            mcap_series.append({"as_of": d, "value": round(est_mcap, 2)})

    latest = dict(anchor)
    latest["口径"] = "指数成交量×均价近似"
    latest["锚点日期"] = anchor["as_of"]
    logger.info(
        "批量成交额: 锚点 %s(官方 %.0f亿), 覆盖 %s ~ %s 共 %d 个交易日",
        anchor["as_of"], anchor["turnover"], days[0], days[-1], len(turnover_series),
    )
    return turnover_series, rate_series, latest, mcap_series


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
    """最近 n 个交易日（含今天）的日期列表，正序。取不到交易日历时退化为工作日。

    交易日历（约 9000 行全表）走轮内缓存：一轮刷新里成交额 / 换手率都会调到这里，
    没必要重复下载同一份日历。
    """
    days: List[str] = []
    today = date.today().strftime("%Y-%m-%d")
    try:
        ak = _ak()
        cal = _cached_snapshot("trade_cal", lambda: ak.tool_trade_date_hist_sina())
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
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
    """融资余额（亿元）与占流通市值比例。

    ⚠️ 口径 2026-10-20 起调整：**只算融资余额，不含融券余额**（原为「融资 + 融券」
    的两融余额）。融券余额长期只占两融合计的 1~2%，剔除后指标更贴近「杠杆资金水位」
    这一真实含义（融券余额是空头头寸，与「市场贪婪」方向相反，混进来会互相抵消）。
    对综合温度计的分档（1.0/1.5/2.5/4.0%）同步沿用，差异 < 0.05pp，不影响判档。

    历史序列存的是**占流通市值百分比**（与指标卡判档同口径）。交易所的逐日流通市值
    没有批量历史源，只能取到「当前这一个点」，因此历史序列里**只有最新那一天**能算出
    真实占比；更早的点拿不到对应日期的流通市值，用一个基于当前流通市值反推的比例会
    把市值的涨跌错误地摊到历史上，曲线形状失真，所以宁可不给。
    """
    ak = _ak()
    df = ak.stock_margin_account_info()
    rows = []
    for _, r in df.iterrows():
        rz = _num(r.get("融资余额"))
        if rz is None:
            continue
        rows.append((_d(r.get("日期")), round(rz, 2)))
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

    # 逐日占比序列：只有「当天能拿到流通市值」的日期才给值。目前只有最新一天满足，
    # 于是历史曲线从这一天天天累积（每次刷新多一个点），而不是靠市值反推伪造历史。
    ratio_series: List[Dict[str, Any]] = []
    if ratio is not None:
        ratio_series = [{"as_of": last["as_of"], "value": ratio}]

    return {
        "value": ratio,
        "as_of": last["as_of"],
        "source": "akshare",
        "detail": {
            "融资余额(万亿)": round(last["value"] / 10000, 4),
            # 当期余额（亿元，原始单位）：供需要与余额序列同量纲的下游使用
            "融资余额(亿)": last["value"],
            "占流通市值%": ratio,
            "流通市值(万亿)": round(float_mcap_yi / 10000, 2) if float_mcap_yi else None,
            "近5日变化%": changes.get("近5个交易日"),
            "近20日变化%": changes.get("近20个交易日"),
            "近60日变化%": changes.get("近60个交易日"),
            "连续回落交易日": streak,
        },
        # ⚠️ 这里**必须**是 ratio_series（占流通市值%），与 value / 指标卡判档同口径。
        # 原实现误写成下面的 series（融资余额亿元），字典后一个 "series" 键覆盖前一个，
        # 导致回填路径把余额写进历史表、刷新路径写占比 —— 同一个 key 混两种量纲。
        # 更隐蔽的后果：下游会把「余额序列」与「占比读数」比分位，量纲错配直接
        # 把分位压到 0、分数顶到 100（greed_percentile 的量纲保护会拦下，但别依赖它）。
        "series": ratio_series,
        # 融资余额序列仅供页面看趋势，不作为任何指标的历史序列落库
        "_balance_series": series,
    }


# ------------------------------------------------------------ 市场宽度

def _fetch_breadth_legu() -> Dict[str, Any]:
    """兜底：乐咕乐股赚钱效应（带限流退避）。"""
    ak = _ak()
    df = _legu_call(ak.stock_market_activity_legu)
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
            "数据源": "乐咕乐股 stock_market_activity_legu（兜底）",
        },
        "series": None,
    }


def fetch_breadth() -> Dict[str, Any]:
    """市场宽度：用腾讯全 A 快照的「涨跌幅」列统计涨跌家数，失败回退乐咕乐股。

    与 fetch_below_na 共用同一次快照请求（_cached_snapshot 轮内缓存）。
    """
    try:
        df = _tx_frame()
        if "zdf" not in df.columns:
            raise RuntimeError(f"腾讯快照无 zdf（涨跌幅）列: {list(df.columns)[:10]}")
        chg = [x for x in (_num(v) for v in df["zdf"]) if x is not None]
        if len(chg) < 1000:
            raise RuntimeError(f"腾讯快照有效涨跌幅样本过少（{len(chg)}）")
        up = sum(1 for x in chg if x > 0)
        down = sum(1 for x in chg if x < 0)
        flat = sum(1 for x in chg if x == 0)
        if (up + down) <= 0:
            raise RuntimeError("腾讯快照涨跌家数全为 0（非交易时段？）")
        return {
            "value": round(up / (up + down) * 100, 1),
            "as_of": date.today().strftime("%Y-%m-%d"),
            "source": "akshare",
            "detail": {
                "上涨家数": up,
                "下跌家数": down,
                "平盘家数": flat,
                "样本家数": len(chg),
                "数据源": "腾讯 stock_zh_a_spot_tx（全A快照自算）",
            },
            "series": None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯快照算市场宽度失败，回退乐咕乐股：%s", e)
        return _fetch_breadth_legu()


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
    pct, pct_window = percentile_rank(value, series, "M")

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
    pct, pct_window = percentile_rank(last["value"], series, "M")

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
    # 科创100 PE-TTM：只展示、不参与任何计算（见 DISPLAY_ONLY_INDICATORS）
    _run("kc100_pe", fetch_kc100_pe)
    _run("hs300_pb", fetch_hs300_pb)

    y10 = None
    try:
        y10 = fetch_y10_series()
    except Exception as e:
        logger.warning("10 年期国债收益率抓取失败: %s", e)
    try:
        pe_payload = out.get("hs300_pe") or {}
        if pe_payload.get("error"):
            raise RuntimeError(f"PE 不可用，ERP 跳过：{pe_payload['error']}")
        out["erp"] = fetch_erp(
            pe_payload.get("series") or [],
            y10,
            pe_now=pe_payload.get("value"),
            pe_as_of=pe_payload.get("as_of"),
        )
    except Exception as e:
        logger.warning("股票指标 erp 抓取失败: %s", e)
        out["erp"] = {"error": str(e)[:200]}

    _run("below_na", fetch_below_na)

    # 情绪层：交易所口径一次取数，供成交额 / 换手率 / 融资余额占比共用
    latest_market = None
    try:
        t_series, r_series, m_series, latest_market = fetch_market_series(exchange_days)
        if t_series:
            t_last = t_series[-1]
            t_pct, t_win = percentile_rank(t_last["value"], t_series, "D")
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
                    "分位窗口": r_win or "无历史样本",
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
    etf_prev = None
    if prev.get("etf_share"):
        etf_prev = prev["etf_share"]["value"] if isinstance(prev["etf_share"], dict) else prev["etf_share"]
    _run("etf_share", fetch_etf_share, etf_prev)
    _run("new_fund", fetch_new_fund)
    _run("industry_capital", fetch_industry_capital, industry_months)

    return out


# ================================================================ 响应组装

def _detail_pairs(detail: dict, skip=("数据源", "分位窗口")) -> List[str]:
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
            return band_of(ind, pct), pct, "按分位判定（现有样本）"
        return None, None, "无历史样本，暂不定档"
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

    ⚠ temp_invert 只作用于**分位**口径，不作用于档位分：
    分位是「原始数值在历史分布里的位置」，高分位未必= 热（ERP 分位越高= 赔率越好= 越冷），
    所以反向指标要把分位映射成 100-pct。而 bands 里的 score **已经是最终热度刻度**
    （破净率 >10% 标 12 分「历史底部区」、产业资本增持潮标 12 分「底部特征」），
    再取 100-s 就是二次反转，会出现「状态写着偏冷、分数却是 70 偏热」的自相矛盾。
    """
    invert = bool(ind.get("temp_invert"))
    if pct is not None:
        return round(100 - pct if invert else pct, 1)
    s, _ = interpolate_score(ind, value)
    if s is None:
        band = band_of(ind, value)
        if band is None:
            return None
        s = float(band.get("score", 50))
    # 档位分已是最终热度刻度，不再取反（见上方说明）
    return round(s, 1)


def _score_with_anchor(ind: Dict[str, Any], value: Optional[float],
                       pct: Optional[float], detail: dict
                       ) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """与 score_from 同一口径，但把打分锚点一并带出来，供页面展示。

    分位口径：锚点就是 (0, 0) 与 (100, 100)（反向指标则翻转）。
    读数口径：锚点是 interpolate_score 算出的相邻两档交界点。
    """
    invert = bool(ind.get("temp_invert"))
    if pct is not None:
        s = 100 - pct if invert else pct
        lo_y, hi_y = (100.0, 0.0) if invert else (0.0, 100.0)
        t = max(0.0, min(1.0, float(pct) / 100.0))
        # 分位口径的锚点固定在 0% / 100%，统一写成整数，不受指标 digits 影响
        return round(s, 1), {"mode": "linear", "lo": (0.0, lo_y), "hi": (100.0, hi_y),
                             "t": round(t, 4), "axis": "%", "axis_label": "分位"}

    x = detail.get("较上次快照变化%") if (ind.get("judge") or "value") == "rate" else value
    s, anchor = interpolate_score(ind, x)
    if s is None:
        band = band_of(ind, value)
        if band is None:
            return None, None
        s = float(band.get("score", 50))
        anchor = {"mode": "single", "score": s}
    if anchor:
        # rate 口径的锚点是「份额周变化率%」，用百分号；其余用指标自身单位
        anchor["axis"] = "%" if (ind.get("judge") == "rate") else (ind.get("unit") or "")
        anchor["axis_label"] = "周变化率" if (ind.get("judge") == "rate") else "读数"
    # 档位分已是最终热度刻度，不再取反（见 score_from 的说明）
    return round(s, 1), anchor


def _score_of(ind: Dict[str, Any], value: Optional[float], pct: Optional[float],
              detail: dict) -> Optional[float]:
    """实时卡片用：按 judge 的口径判档，再折算热度分（ETF 份额用「周变化率」判档）。"""
    mode = ind.get("judge") or "value"
    if mode == "rate":
        s, _ = interpolate_score(ind, detail.get("较上次快照变化%"))
        return round(float(s), 1) if s is not None else None
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

    heat_score, anchor = _score_with_anchor(ind, value, pct, detail)
    # 读数档位被插值细化后，原band 不再精确描述该读数所处的"档内位置"，
    # 状态列仍沿用档位判读（状态是离散结论，单项分才是连续量）。

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
        "heat_score": heat_score,
        "score_anchor": anchor,
        "anchors": [
            {"value": round(x, 4), "score": round(y, 2)}
            for x, y in interpolation_anchors(ind)
        ] if (ind.get("temp_weight") or 0) > 0 else [],
    }
    return item


def build_thermometer(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """按「估值 ×35% + 情绪 ×35% + 资金 ×30%」合成 0-100 市场温度计。"""
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
        "formula": "综合温度 = 估值温度 ×35% + 情绪温度 ×35% + 资金温度 ×30%",
        "coverage": round(
            sum(l["score"] is not None for l in layer_out) / len(layer_out) * 100, 0
        ),
    }


def build_response(raw: Dict[str, dict]) -> dict:
    """把库中读数（raw）组装成页面需要的结构。"""
    items: Dict[str, Dict[str, Any]] = {}
    for ind in INDICATORS:
        items[ind["key"]] = build_item(ind, raw.get(ind["key"]))

    # 仅展示指标（科创100 PE-TTM）：同样 build 成 item 塞进 items，
    # 好让贪婪指数拿到和沪深300 PE 完全一致的一套字段（value/as_of/state/分位/含义）。
    # 但**不进 groups**（分组表按 INDICATOR_KEYS 遍历），也不参与温度计合成。
    for ind in DISPLAY_ONLY_INDICATORS:
        items[ind["key"]] = build_item(ind, raw.get(ind["key"]))

    groups = []
    for gkey in (GROUP_VALUATION, GROUP_SENTIMENT, GROUP_CAPITAL):
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

    # 市场贪婪指数（V3.6c 口径）：复用同一批 items，方向与综合温度计一致（越高越贪婪）。
    # 细节见 app/utils/greed_index.py —— 分位窗口近 13 期，与综合温度计的近十年刻意不同。
    greed_index = greed_index_mod.build_greed_index(items)

    # 温度计历史（用于趋势图）：由分位/热度分逐期重算，见 stock_temp_refresh
    return {
        "status": "ok",
        "tones": TONES,
        "groups": groups,
        "thermometer": thermo,
        "greed_index": greed_index,
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
    if not pts:
        return []
    vals = [p["value"] for p in pts]
    cap = PCT_WINDOW_POINTS["M"]
    out = []
    for i in range(len(pts)):
        windowed = vals[max(0, i + 1 - cap): i + 1]
        if len(windowed) < 2:  # 同上：单点分位恒 100%
            continue
        # 与 percentile_rank 同口径：中位秩（等于按半数计），避免重复值顶到 100%
        cur = vals[i]
        less = sum(1 for v in windowed if v < cur - 1e-9)
        equal = sum(1 for v in windowed if abs(v - cur) <= 1e-9)
        out.append({"as_of": pts[i]["as_of"],
                    "value": round((less + equal / 2) / len(windowed) * 100, 1)})
    return out


def reconstruct_temperature(series_map: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """用历史表里的序列重建「月度温度」轨迹。

    每条序列先按月取样；分位能算出来的（PE / PB / ERP / 破净率 / 新基金 …
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
            "分位为逐点滚动口径（每点只用当时可见的历史，不设最小样本期数）；取不到分位的指标退回按读数档位打分，"
            "并按该月可得数据标注覆盖。三层齐备才给综合温度：日频情绪指标的历史由本页逐日积累，"
            "所以综合温度线会从「三个层都有数据」的月份才开始出现。"
        ),
    }


# ------------------------------------------------- 每日综合温度打卡（读库口径）
#
# 「热度趋势」页已改为「只展示各指标曲线、数据从现在开始逐日积累」，不再做历史回溯、
# 不再实时重建月度轨迹。每次刷新抓取成功后会由 stock_temp_refresh._record_temperature()
# 写入一条 key="temperature" 的打卡（value=综合温度，detail 里带三层温度与分离度），
# /api/stock-temp/series 直接读这些打卡，前端按日画曲线。

TEMP_TRACK_KEYS = ("valuation", "sentiment", "capital", "total")

# 打卡 detail 里三层温度用的键名（与 TEMP_LAYERS 的 name 对应）
TEMP_LAYER_NAMES = {layer["key"]: layer["name"] for layer in TEMP_LAYERS}
