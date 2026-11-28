"use strict";
/**
 * 股票温度（市场贪婪指数 0-100 · 对标 CNN Fear & Greed）—— 纯计算层。
 * 忠实移植自 app/utils/stock_temp.py：
 *  - 13 项指标元数据 / 分档 / 权重
 *  - 三层温度计（估值×50% + 情绪×30% + 资金×20%）
 *  - 贪婪指数（情绪×88% + 估值×12%）+ 确认层闸门
 *  - 月度温度历史重建
 *
 * 与原版的唯一差异：
 *  1. 删除 FALLBACK_DEFAULTS（AC-12：绝不种子兜底值，拉不到就不显示）。
 *  2. score_from 在 judge="percentile" 且分位拿不到时返回 null（原版会退回用读数去匹配
 *     分位数档，是静默错误；原版 judge() 已显式警告，这里在打分侧一并收口）。
 */

// ---------------------------------------------------------------- 分组与色调
const GROUP_VALUATION = "valuation";
const GROUP_SENTIMENT = "sentiment";
const GROUP_CAPITAL = "capital";
const GROUP_STRUCTURE = "structure";

const GROUP_TITLES = {
  [GROUP_VALUATION]: "① 估值层 · 贵不贵",
  [GROUP_SENTIMENT]: "② 情绪层 · 亢奋还是低迷",
  [GROUP_CAPITAL]: "③ 资金层 · 钱进不进",
  [GROUP_STRUCTURE]: "④ 结构 · 热度在哪个市场",
};

const GROUP_SUBTITLES = {
  [GROUP_VALUATION]: "月频慢变量 · 决定能不能长期拿",
  [GROUP_SENTIMENT]: "日频快变量 · 决定短线热不热",
  [GROUP_CAPITAL]: "周频/月频 · 决定行情有没有持续性",
  [GROUP_STRUCTURE]: "观察分化：同一个 A 股，热度可能不在一个市场",
};

const GROUP_TONES = {
  [GROUP_VALUATION]: "blue",
  [GROUP_SENTIMENT]: "amber",
  [GROUP_CAPITAL]: "green",
  [GROUP_STRUCTURE]: "violet",
};

const TONES = {
  blue: "#3b82f6",
  cyan: "#06b6d4",
  green: "#22c55e",
  lime: "#84cc16",
  amber: "#f59e0b",
  orange: "#f97316",
  red: "#ef4444",
  violet: "#8b5cf6",
  slate: "#64748b",
};

const SOURCE_LABELS = {
  akshare: "自动抓取",
  eastmoney: "东方财富",
  em: "东方财富",
  legulegu: "乐咕乐股",
  csindex: "中证指数",
  sina: "新浪财经",
};

// ---------------------------------------------------------------- 温度计分层
const TEMP_LAYERS = [
  {
    key: GROUP_VALUATION,
    name: "估值温度",
    weight: 0.5,
    tone: "blue",
    desc: "PE 分位 40% + ERP 30% + 破净率 30%。慢变量，定方向。",
  },
  {
    key: GROUP_SENTIMENT,
    name: "情绪温度",
    weight: 0.3,
    tone: "amber",
    desc: "成交额分位 50% + 换手率分位 30% + 两融占流通市值 20%。快变量，定情绪。",
  },
  {
    key: GROUP_CAPITAL,
    name: "资金温度",
    weight: 0.2,
    tone: "green",
    desc: "公募仓位 30% + 股票ETF份额 25% + 新基金发行 25% + 产业资本 20%。资金变量，定底气。",
  },
];

const TEMP_ZONES = [
  { lo: 0, hi: 20, state: "冰点 · 布局区", tone: "blue" },
  { lo: 20, hi: 40, state: "偏冷", tone: "cyan" },
  { lo: 40, hi: 60, state: "中性", tone: "lime" },
  { lo: 60, hi: 80, state: "偏热", tone: "orange" },
  { lo: 80, hi: 100.01, state: "亢奋 · 风险区", tone: "red" },
];

// ---------------------------------------------------------------- 市场贪婪指数
const GREED_SAMPLE_DAYS = 15;
const GREED_MIN_SAMPLE = 5;
const GREED_SENTIMENT_WEIGHT = 0.88;
const GREED_VALUATION_WEIGHT = 0.12;

const GREED_COMPONENTS = [
  { key: "breadth", category: "sentiment", name: "市场宽度" },
  { key: "turnover_rate", category: "sentiment", name: "全A换手率" },
  { key: "turnover_amt", category: "sentiment", name: "两市成交额" },
  { key: "hs300_pe", category: "valuation", name: "沪深300 PE-TTM" },
];
const GREED_KEYS = GREED_COMPONENTS.map((c) => c.key);
const GREED_CONFIRM_KEYS = ["erp", "below_na"];

const GREED_ZONES = [
  { lo: 0, hi: 10, state: "极端恐慌 · 机会区", tone: "blue", color: "#5B8FC7", desc: "情绪冰点，往往对应可布局区域" },
  { lo: 10, hi: 30, state: "恐慌 · 备资金", tone: "cyan", color: "#8BC8EA", desc: "偏冷，留足子弹，等待确认" },
  { lo: 30, hi: 70, state: "中性 · 无信号", tone: "lime", color: "#E4D48F", desc: "多空平衡，不做方向性押注" },
  { lo: 70, hi: 90, state: "贪婪 · 不追加", tone: "orange", color: "#F4B393", desc: "情绪偏热，持有但不追高" },
  { lo: 90, hi: 100.01, state: "极端贪婪 · 泡沫", tone: "red", color: "#EA6668", desc: "热 + 贵共振，警惕均值回归" },
];

// ---------------------------------------------------------------- 分位窗口
const PCT_WINDOW_POINTS = { M: 120, W: 520, D: 2500 };
const FREQ_LABELS = { M: "近十年（120 个月）", W: "近十年（520 周）", D: "近十年（2500 个交易日）" };

const CHART_KEYS = [
  "hs300_pe", "hs300_pb", "erp", "below_na",
  "turnover_amt", "turnover_rate", "margin", "total_float_mcap", "breadth",
  "fund_position", "new_fund", "industry_capital", "etf_share",
  "structure_div", "temperature", "greed",
];

// ---------------------------------------------------------------- 指标清单
const INDICATORS = [
  // ① 估值层
  {
    key: "hs300_pe", group: GROUP_VALUATION, name: "沪深300 PE-TTM", subtitle: "宽基估值：贵不贵",
    unit: "倍", digits: 2, temp_layer: GROUP_VALUATION, temp_weight: 0.4, temp_invert: false,
    freq: "月频", judge: "percentile",
    meaning: "沪深300 指数滚动市盈率（PE-TTM，市值加权），是宽基指数最常用的估值锚。这里不看绝对值，只看它在过去十年分布里的位置（分位）。",
    guide: "十年分位 <20% 极冷（便宜）、>80% 极热（贵）。⚠ 看 PE 先拆分母：盈利下行会「被动抬升」PE——分母（E）缩水而不是分子（P）涨，这种「贵」是假贵，必须配 PB 交叉验证。",
    warning: "PE 分位偏高时先看是不是盈利下行「分母收缩」造成的，务必配 PB 一起读。",
    bands: [
      { lo: null, hi: 20, range: "分位 < 20%", state: "极冷 · 便宜", tone: "blue", score: 10 },
      { lo: 20, hi: 40, range: "分位 20 ~ 40%", state: "偏冷", tone: "cyan", score: 30 },
      { lo: 40, hi: 60, range: "分位 40 ~ 60%", state: "中性", tone: "lime", score: 50 },
      { lo: 60, hi: 80, range: "分位 60 ~ 80%", state: "偏热", tone: "orange", score: 70 },
      { lo: 80, hi: null, range: "分位 > 80%", state: "极热 · 贵", tone: "red", score: 90 },
    ],
  },
  {
    key: "hs300_pb", group: GROUP_VALUATION, name: "沪深300 PB", subtitle: "PB 交叉验证 PE",
    unit: "倍", digits: 2, temp_layer: GROUP_VALUATION, temp_weight: 0.0, temp_invert: false,
    freq: "月频", judge: "percentile",
    meaning: "沪深300 指数市净率（PB）。净资产（B）比盈利（E）稳定得多，所以 PB 是判断「估值底部有没有支撑」的交叉验证工具。",
    guide: "PB 分位与 PE 分位背离时，以 PB 为准：PE 高 + PB 低 = 盈利周期底部（分母收缩造成的假贵），而不是真的贵。",
    warning: "PE 与 PB 分位背离时，PB 更可信——净资产比盈利稳定。",
    bands: [
      { lo: null, hi: 20, range: "分位 < 20%", state: "极冷 · 估值底部", tone: "blue", score: 10 },
      { lo: 20, hi: 40, range: "分位 20 ~ 40%", state: "偏低", tone: "cyan", score: 30 },
      { lo: 40, hi: 60, range: "分位 40 ~ 60%", state: "中性", tone: "lime", score: 50 },
      { lo: 60, hi: 80, range: "分位 60 ~ 80%", state: "偏高", tone: "orange", score: 70 },
      { lo: 80, hi: null, range: "分位 > 80%", state: "极热 · 贵", tone: "red", score: 90 },
    ],
  },
  {
    key: "erp", group: GROUP_VALUATION, name: "ERP 股债性价比", subtitle: "1/PE − 10 年期国债收益率",
    unit: "%", digits: 2, temp_layer: GROUP_VALUATION, temp_weight: 0.3, temp_invert: true,
    freq: "月频", judge: "percentile",
    meaning: "股权风险溢价 ERP = 1/PE − 10 年期国债收益率，衡量「买股票相对买国债多拿多少赔率」。数值越高 = 股票越便宜、赔率越好。",
    guide: "看十年分位：>80% 说明赔率仍在好的一侧（偏冷、便宜）；<20% 说明股票相对债券已无性价比（偏热）。历史上 ERP 进入极低分位后，权益的中期收益普遍不佳。",
    warning: "ERP 是「相对」指标：无风险利率大幅下行时，ERP 走高不等于股票立刻会涨，只说明赔率改善。",
    bands: [
      { lo: null, hi: 20, range: "分位 < 20%", state: "贵 · 相对债券无性价比", tone: "red", score: 88 },
      { lo: 20, hi: 40, range: "分位 20 ~ 40%", state: "偏贵", tone: "orange", score: 70 },
      { lo: 40, hi: 60, range: "分位 40 ~ 60%", state: "中性", tone: "lime", score: 50 },
      { lo: 60, hi: 80, range: "分位 60 ~ 80%", state: "偏便宜 · 偏冷", tone: "cyan", score: 30 },
      { lo: 80, hi: null, range: "分位 > 80%", state: "赔率好 · 冷", tone: "blue", score: 12 },
    ],
  },
  {
    key: "below_na", group: GROUP_VALUATION, name: "破净率", subtitle: "全部 A 股股价跌破每股净资产的比例",
    unit: "%", digits: 2, temp_layer: GROUP_VALUATION, temp_weight: 0.3, temp_invert: true,
    freq: "日频", judge: "value",
    meaning: "全部 A 股中「股价 < 每股净资产」的公司占比（含 B 股口径为全部 A 股）。破净是深度低估、悲观定价的集中体现，是典型的底部区域温度计。",
    guide: ">10% 属历史底部区；3 ~ 6% 中性；<3% 说明市场已经很难找到便宜货，偏热。",
    bands: [
      { lo: 10, hi: null, range: "> 10%", state: "历史底部区", tone: "blue", score: 12 },
      { lo: 6, hi: 10, range: "6 ~ 10%", state: "偏冷", tone: "cyan", score: 30 },
      { lo: 3, hi: 6, range: "3 ~ 6%", state: "中性", tone: "lime", score: 52 },
      { lo: 1, hi: 3, range: "1 ~ 3%", state: "偏热", tone: "orange", score: 72 },
      { lo: null, hi: 1, range: "< 1%", state: "极热 · 无便宜货", tone: "red", score: 88 },
    ],
  },

  // ② 情绪层
  {
    key: "turnover_amt", group: GROUP_SENTIMENT, name: "两市成交额", subtitle: "沪 + 深 股票成交金额",
    unit: "亿元", digits: 0, temp_layer: GROUP_SENTIMENT, temp_weight: 0.5, temp_invert: false,
    freq: "日频", judge: "value",
    meaning: "沪市 + 深市股票成交金额合计，是市场活跃度最直接的度量。取自东财 A 股全市场行情汇总口径。",
    guide: "成交额只说明「热不热」，不说明「贵不贵」：<7000 亿属地量冰点；2 万亿以上是亢奋区；3 万亿以上属极端。地量往往对应底部区域的成交量特征。",
    warning: "成交额 2 万亿 ≠ 市场便宜。它是情绪温度，不是估值温度，两层必须分开看。",
    bands: [
      { lo: null, hi: 7000, range: "< 7000 亿", state: "地量 · 冰点", tone: "blue", score: 8 },
      { lo: 7000, hi: 12000, range: "7000 ~ 12000 亿", state: "偏冷", tone: "cyan", score: 28 },
      { lo: 12000, hi: 18000, range: "12000 ~ 18000 亿", state: "中性", tone: "lime", score: 50 },
      { lo: 18000, hi: 25000, range: "18000 ~ 25000 亿", state: "活跃 · 偏热", tone: "orange", score: 72 },
      { lo: 25000, hi: 30000, range: "25000 ~ 30000 亿", state: "亢奋", tone: "red", score: 86 },
      { lo: 30000, hi: null, range: "> 30000 亿", state: "极端", tone: "red", score: 96 },
    ],
  },
  {
    key: "turnover_rate", group: GROUP_SENTIMENT, name: "全A换手率", subtitle: "两市成交额 ÷ 两市流通市值",
    unit: "%", digits: 2, temp_layer: GROUP_SENTIMENT, temp_weight: 0.3, temp_invert: false,
    freq: "日频", judge: "value",
    meaning: "全市场换手率 = 两市股票成交金额 ÷ 两市股票流通市值。它把「成交额随市值自然变大」的因素剔除掉，是比成交额更干净的情绪度量。",
    guide: "经验区间：<1% 冰点 / 1~1.5% 偏冷 / 1.5~2.5% 中性 / 2.5~4% 偏热 / >4% 亢奋。",
    warning: "日频历史由本页逐日积累，分位窗口会在详情里标注实际样本量。",
    bands: [
      { lo: null, hi: 1.0, range: "< 1.0%", state: "地量 · 冰点", tone: "blue", score: 10 },
      { lo: 1.0, hi: 1.5, range: "1.0 ~ 1.5%", state: "偏冷", tone: "cyan", score: 30 },
      { lo: 1.5, hi: 2.5, range: "1.5 ~ 2.5%", state: "中性", tone: "lime", score: 50 },
      { lo: 2.5, hi: 4.0, range: "2.5 ~ 4.0%", state: "偏热", tone: "orange", score: 72 },
      { lo: 4.0, hi: null, range: "> 4.0%", state: "亢奋", tone: "red", score: 90 },
    ],
  },
  {
    key: "margin", group: GROUP_SENTIMENT, name: "两融占流通市值", subtitle: "融资余额 + 融券余额，占两市流通市值比例",
    unit: "%", digits: 2, temp_layer: GROUP_SENTIMENT, temp_weight: 0.2, temp_invert: false,
    temp_reconstruct: false,
    freq: "日频", judge: "value",
    meaning: "融资融券余额（杠杆资金规模）占两市流通市值的比重，衡量市场杠杆水位。余额由沪深两市账户信息合并口径取得，流通市值取东财全市场汇总。",
    guide: ">3.5% 杠杆过热；2.5 ~ 3.5% 中性区间；<2.5% 杠杆低迷。看趋势比看点位更重要：连续回落 = 杠杆在退潮，行情持续性存疑。",
    bands: [
      { lo: null, hi: 2.0, range: "< 2.0%", state: "杠杆低迷", tone: "blue", score: 15 },
      { lo: 2.0, hi: 2.5, range: "2.0 ~ 2.5%", state: "偏低", tone: "cyan", score: 32 },
      { lo: 2.5, hi: 3.0, range: "2.5 ~ 3.0%", state: "中性", tone: "lime", score: 50 },
      { lo: 3.0, hi: 3.5, range: "3.0 ~ 3.5%", state: "偏高", tone: "orange", score: 70 },
      { lo: 3.5, hi: null, range: "> 3.5%", state: "杠杆过热", tone: "red", score: 88 },
    ],
  },
  {
    key: "breadth", group: GROUP_SENTIMENT, name: "市场宽度", subtitle: "上涨家数 ÷（上涨 + 下跌）",
    unit: "%", digits: 1, temp_layer: null, temp_weight: 0.0, temp_invert: false,
    freq: "日频", judge: "value",
    meaning: "当日上涨家数占（上涨 + 下跌）家数的比例，反映赚钱效应是「普涨」还是「普跌」。指数可以靠权重股拉抬，宽度不会骗人。",
    guide: ">80% 普涨（情绪亢奋）；40 ~ 60% 结构性；<30% 普跌（恐慌或缩量阴跌）。",
    warning: "指数涨而宽度差 = 只有权重在涨，是典型的「赚指数不赚钱」，热度集中在少数板块。",
    bands: [
      { lo: 80, hi: null, range: "> 80%", state: "普涨", tone: "red", score: 88 },
      { lo: 60, hi: 80, range: "60 ~ 80%", state: "偏强", tone: "orange", score: 70 },
      { lo: 40, hi: 60, range: "40 ~ 60%", state: "均衡 · 结构性", tone: "lime", score: 50 },
      { lo: 30, hi: 40, range: "30 ~ 40%", state: "偏弱", tone: "cyan", score: 32 },
      { lo: null, hi: 30, range: "< 30%", state: "普跌", tone: "blue", score: 12 },
    ],
  },

  // ③ 资金层
  {
    key: "fund_position", group: GROUP_CAPITAL, name: "公募仓位", subtitle: "股票型基金仓位（周频）",
    unit: "%", digits: 2, temp_layer: GROUP_CAPITAL, temp_weight: 0.3, temp_invert: false,
    freq: "周频", judge: "percentile",
    meaning: "股票型基金的股票投资占净值比例（周频估算）。仓位越高说明公募可加仓的子弹越少、越拥挤，即所谓「仓位魔咒」（高仓 = 拥挤）。",
    guide: "该口径（股票型基金）长期仓位偏高（常年 90% 上下），所以看十年分位比看绝对水平更有意义：分位 >80% 属高仓拥挤，<20% 属明显低仓、子弹充足。",
    warning: "「88% 仓位魔咒」主要指偏股混合型；股票型基金因合同下限（≥80%）常年高位，请以十年分位为准。",
    bands: [
      { lo: null, hi: 20, range: "分位 < 20%", state: "低仓 · 子弹充足", tone: "blue", score: 12 },
      { lo: 20, hi: 40, range: "分位 20 ~ 40%", state: "偏低", tone: "cyan", score: 30 },
      { lo: 40, hi: 60, range: "分位 40 ~ 60%", state: "中性", tone: "lime", score: 50 },
      { lo: 60, hi: 80, range: "分位 60 ~ 80%", state: "偏高", tone: "orange", score: 70 },
      { lo: 80, hi: null, range: "分位 > 80%", state: "高仓拥挤", tone: "red", score: 88 },
    ],
  },
  {
    key: "etf_share", group: GROUP_CAPITAL, name: "股票ETF份额", subtitle: "宽基股票ETF 合计份额及其变化",
    unit: "亿份", digits: 0, temp_layer: GROUP_CAPITAL, temp_weight: 0.25, temp_invert: false,
    freq: "日频（快照）", judge: "rate",
    meaning: "沪深300 / 中证500 / 中证1000 / 上证50 / 科创50 / 创业板指 / 中证A500 等宽基股票ETF 的合计份额。份额持续申购 = 机构（含国家队）在借道 ETF 进场，是最典型的逆势信号。",
    guide: "看份额的周变化方向：份额逆势增（指数跌、份额增）是最强的底部特征；份额持续赎回而指数在涨，说明反弹缺乏机构资金承接。",
    warning: "份额为每日快照，变化需本页逐日积累后才可读；首次运行时只展示绝对水平。",
    bands: [
      { lo: 1.0, hi: null, range: "周变化 > +1.0%", state: "持续申购 · 资金进场", tone: "blue", score: 15 },
      { lo: 0.2, hi: 1.0, range: "周变化 +0.2 ~ +1.0%", state: "小幅净申购", tone: "cyan", score: 35 },
      { lo: -0.2, hi: 0.2, range: "周变化 −0.2 ~ +0.2%", state: "持平", tone: "lime", score: 50 },
      { lo: -1.0, hi: -0.2, range: "周变化 −1.0 ~ −0.2%", state: "小幅赎回", tone: "orange", score: 68 },
      { lo: null, hi: -1.0, range: "周变化 < −1.0%", state: "持续赎回", tone: "red", score: 85 },
    ],
  },
  {
    key: "new_fund", group: GROUP_CAPITAL, name: "新基金发行", subtitle: "近 30 日成立的权益类基金募集份额合计",
    unit: "亿元", digits: 0, temp_layer: GROUP_CAPITAL, temp_weight: 0.25, temp_invert: false,
    freq: "月频", judge: "percentile",
    meaning: "近 30 日新成立基金中「股票型 + 混合型 + 指数型-股票」的募集份额合计（1 亿元 = 1 亿份）。新发是散户与渠道情绪的镜子：冰点（几十亿）出现在底部，千亿抢购往往出现在阶段性顶部。",
    guide: "看月度序列的分位：分位低 = 发行冰点（往往对应底部区域）；分位高 = 发行火爆（顶部信号）。同时看近 90 日/近一年的量级，避免单月波动误导。",
    warning: "「千亿抢购」是顶部特征，不是利好——发行越火，后续增量资金越少。",
    bands: [
      { lo: null, hi: 20, range: "分位 < 20%", state: "发行冰点 · 底部特征", tone: "blue", score: 12 },
      { lo: 20, hi: 40, range: "分位 20 ~ 40%", state: "偏冷", tone: "cyan", score: 30 },
      { lo: 40, hi: 60, range: "分位 40 ~ 60%", state: "中性", tone: "lime", score: 50 },
      { lo: 60, hi: 80, range: "分位 60 ~ 80%", state: "偏热", tone: "orange", score: 70 },
      { lo: 80, hi: null, range: "分位 > 80%", state: "千亿抢购 · 顶部信号", tone: "red", score: 90 },
    ],
  },
  {
    key: "industry_capital", group: GROUP_CAPITAL, name: "产业资本净增持", subtitle: "近 30 日重要股东增减持净额",
    unit: "亿元", digits: 1, temp_layer: GROUP_CAPITAL, temp_weight: 0.2, temp_invert: true,
    freq: "月频", judge: "value",
    meaning: "重要股东（高管、大股东、产业资本）二级市场及大宗交易增减持的净额（增持为正、减持为负），按变动数量 × 变动价格估算成金额。产业资本离公司基本面最近，被称作「最聪明的钱」。",
    guide: "净增持（尤其逆势增持潮）= 冷 / 底部特征；净减持放大（减持潮）= 热 / 需要警惕。看月度序列的「由负转正」转折，比看单月金额更有意义。",
    bands: [
      { lo: 100, hi: null, range: "净增持 > 100 亿", state: "增持潮 · 底部特征", tone: "blue", score: 12 },
      { lo: 0, hi: 100, range: "净增持 0 ~ 100 亿", state: "小幅净增持", tone: "cyan", score: 32 },
      { lo: -150, hi: 0, range: "净减持 0 ~ 150 亿", state: "小幅净减持", tone: "lime", score: 52 },
      { lo: -400, hi: -150, range: "净减持 150 ~ 400 亿", state: "净减持", tone: "orange", score: 72 },
      { lo: null, hi: -400, range: "净减持 > 400 亿", state: "减持潮 · 偏热", tone: "red", score: 88 },
    ],
  },

  // ④ 结构
  {
    key: "structure_div", group: GROUP_STRUCTURE, name: "科创50 vs 创业板 · 分位差", subtitle: "同期价格分位之差（pp），衡量热度是否在同一市场",
    unit: "pp", digits: 1, temp_layer: null, temp_weight: 0.0, temp_invert: false,
    freq: "日频", judge: "value",
    meaning: "科创50 指数价格所处历史分位减去创业板指价格分位。差值为正说明资金集中在科创板（硬科技），为负说明热度在创业板（成长/新能源/医药等）。",
    guide: "分化超过 40pp 属极端分化：涨幅由单一市场驱动，属于结构性行情而不是全面牛。极端分化之后，往往出现方向切换（补涨或高位板块回撤）。",
    warning: "这里用的是「价格分位」而非 PE 分位（科创50 上市时间较短，估值序列覆盖不足）。",
    bands: [
      { lo: 40, hi: null, range: "> +40pp", state: "极端分化 · 单市场独热", tone: "red", score: 90 },
      { lo: 20, hi: 40, range: "+20 ~ +40pp", state: "明显分化", tone: "orange", score: 70 },
      { lo: -20, hi: 20, range: "−20 ~ +20pp", state: "相对均衡", tone: "lime", score: 50 },
      { lo: -40, hi: -20, range: "−40 ~ −20pp", state: "反向分化", tone: "cyan", score: 30 },
      { lo: null, hi: -40, range: "< −40pp", state: "极端反向 · 成长独热", tone: "blue", score: 10 },
    ],
  },
];

const INDICATOR_MAP = {};
for (const ind of INDICATORS) INDICATOR_MAP[ind.key] = ind;
const INDICATOR_KEYS = INDICATORS.map((i) => i.key);
const AUTO_KEYS = INDICATOR_KEYS;
const TEMP_KEYS = INDICATORS.filter((i) => i.temp_weight).map((i) => i.key);

// ================================================================ 通用工具
function num(v) {
  if (v === null || v === undefined || v === "" || v === "-") return null;
  const f = Number(v);
  if (!Number.isFinite(f)) return null;
  return f;
}

function toDateStr(v) {
  if (v === null || v === undefined) return null;
  if (v instanceof Date) {
    return v.toISOString().slice(0, 10);
  }
  const s = String(v).trim();
  if (!s || ["nan", "nat", "none"].includes(s.toLowerCase())) return null;
  return s.slice(0, 10);
}

function cleanSeries(rows) {
  const bag = {};
  for (const [as_of, value] of rows) {
    if (!as_of || value === null || value === undefined) continue;
    bag[String(as_of)] = value;
  }
  return Object.keys(bag)
    .sort()
    .map((k) => ({ as_of: k, value: bag[k] }));
}

function percentileRank(value, series, freq = "M") {
  const vals = (series || []).map((x) => x.value).filter((v) => v !== null && v !== undefined);
  if (value === null || value === undefined || vals.length === 0) return [null, null];
  const cap = PCT_WINDOW_POINTS[freq] || 120;
  const windowed = vals.length > cap ? vals.slice(-cap) : vals;
  if (windowed.length < 12) return [null, `样本仅 ${windowed.length} 期，不足 12 期`];
  const below = windowed.filter((v) => v <= value).length;
  const pct = Math.round((below / windowed.length) * 1000) / 10;
  let label;
  if (windowed.length >= cap) {
    label = FREQ_LABELS[freq] || "近十年";
  } else {
    const firstIdx = series.length - windowed.length;
    const firstAsOf = series[firstIdx] ? series[firstIdx].as_of : "?";
    label = `自 ${firstAsOf} 起共 ${windowed.length} 期`;
  }
  return [pct, label];
}

function bandOf(ind, value) {
  if (value === null || value === undefined) return null;
  for (const b of ind.bands || []) {
    const lo = b.lo;
    const hi = b.hi;
    if ((lo === null || lo === undefined || value >= lo) && (hi === null || hi === undefined || value < hi)) {
      return b;
    }
  }
  return null;
}

function samplePercentile(value, series, days = GREED_SAMPLE_DAYS, minN = GREED_MIN_SAMPLE) {
  const pts = (series || []).filter((p) => p.value !== null && p.value !== undefined);
  const windowed = pts.slice(-days);
  const n = windowed.length;
  if (value === null || value === undefined || n < minN) return [null, n];
  const below = windowed.filter((p) => p.value < value).length;
  return [Math.round((below / n) * 1000) / 10, n];
}

function greedZoneOf(score) {
  if (score === null || score === undefined) return null;
  for (const z of GREED_ZONES) {
    if (z.lo <= score && score < z.hi) return z;
  }
  return GREED_ZONES[GREED_ZONES.length - 1];
}

function dumpDetail(detail) {
  try {
    return JSON.stringify(detail || {});
  } catch (_e) {
    return "{}";
  }
}

function loadDetail(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === "object" ? v : {};
  } catch (_e) {
    return {};
  }
}

// ================================================================ 响应组装
function detailPairs(detail, skip = ["数据源", "分位窗口", "科创50窗口", "创业板窗口"]) {
  const parts = [];
  for (const [k, v] of Object.entries(detail || {})) {
    if (skip.includes(k) || v === null || v === undefined || v === "") continue;
    let text;
    if (typeof v === "number") {
      text = Math.abs(v) >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : String(Math.round(v * 1000) / 1000);
    } else {
      text = String(v);
    }
    parts.push(`${k} ${text}`);
  }
  return parts;
}

function judge(ind, value, pct, detail) {
  const mode = ind.judge || "value";
  if (mode === "percentile") {
    if (pct !== null && pct !== undefined) return [bandOf(ind, pct), pct, "按十年分位判定"];
    return [null, null, "分位样本不足（需 ≥12 期历史），暂不定档"];
  }
  if (mode === "rate") {
    const rate = detail["较上次快照变化%"];
    if (rate === null || rate === undefined) return [null, null, "份额变化需历史积累（首次快照）"];
    return [bandOf(ind, rate), rate, "按份额周变化率判定"];
  }
  return [bandOf(ind, value), value, "按读数判定"];
}

function scoreFrom(ind, value, pct) {
  const invert = !!ind.temp_invert;
  if (pct !== null && pct !== undefined) {
    return Math.round((invert ? 100 - pct : pct) * 10) / 10;
  }
  // 原版在此处退回 band_of(value)；对 judge="percentile" 的指标，这会用读数去匹配 0-100 的分位数档，
  // 是静默错误（原版 judge() 已显式警告）。本实现直接排除，等历史积累到 ≥12 期后由分位打分。
  if ((ind.judge || "value") === "percentile") return null;
  const band = bandOf(ind, value);
  if (!band) return null;
  const s = Number(band.score) || 50;
  return Math.round((invert ? 100 - s : s) * 10) / 10;
}

function scoreOf(ind, value, pct, detail) {
  const mode = ind.judge || "value";
  if (mode === "rate") {
    const band = bandOf(ind, detail["较上次快照变化%"]);
    return band ? Math.round((Number(band.score) || 50) * 10) / 10 : null;
  }
  return scoreFrom(ind, value, pct);
}

function buildItem(ind, rec) {
  rec = rec || {};
  const detail = rec.detail || {};
  let value = rec.value;
  let pct = detail["分位"];
  if (pct !== undefined && pct !== null) {
    const f = Number(pct);
    pct = Number.isFinite(f) ? f : null;
  } else {
    pct = null;
  }

  const [band, judged, basis] = judge(ind, value, pct, detail);
  const tone = band ? band.tone : "slate";
  const source = rec.source || "eastmoney";

  const bands = (ind.bands || []).map((b) => ({
    range: b.range,
    state: b.state,
    note: b.note,
    tone: b.tone,
    active: band !== null && b === band,
  }));

  return {
    key: ind.key,
    group: ind.group,
    name: ind.name,
    subtitle: ind.subtitle,
    unit: ind.unit || "",
    digits: ind.digits || 1,
    freq: ind.freq,
    value,
    as_of: rec.as_of || "—",
    source,
    source_label: SOURCE_LABELS[source] || source,
    error: rec.error,
    missing: value === null || value === undefined,
    percentile: pct,
    percentile_window: detail["分位窗口"],
    judge_value: judged,
    judge_basis: basis,
    state: band ? band.state : (value === null || value === undefined ? "数据缺失" : "—"),
    tone,
    bands,
    detail_text: detailPairs(detail).join(" · "),
    detail,
    meaning: ind.meaning,
    guide: ind.guide,
    warning: ind.warning,
    temp_layer: ind.temp_layer,
    temp_weight: ind.temp_weight || 0.0,
    temp_invert: !!ind.temp_invert,
    heat_score: scoreOf(ind, value, pct, detail),
  };
}

function buildThermometer(items) {
  const layerOut = [];
  for (const layer of TEMP_LAYERS) {
    const members = Object.values(items).filter(
      (it) => it.temp_layer === layer.key && (it.temp_weight || 0) > 0
    );
    const wsum = members.reduce((a, it) => a + it.temp_weight, 0);
    const got = members.map((it) => [it, it.temp_weight, it.heat_score]);
    const usable = got.filter(([, , s]) => s !== null && s !== undefined);
    const usableW = usable.reduce((a, [, w]) => a + w, 0);
    let score = null;
    if (usableW > 0) {
      score = Math.round((usable.reduce((a, [it, w]) => a + it.heat_score * w, 0) / usableW) * 10) / 10;
    }
    layerOut.push({
      key: layer.key,
      name: layer.name,
      weight: layer.weight,
      tone: layer.tone,
      desc: layer.desc,
      score,
      coverage: wsum ? Math.round((usableW / wsum) * 100) : 0,
      members: got.map(([it, w, s]) => ({
        key: it.key,
        name: it.name,
        weight: w,
        score: s,
        percentile: it.percentile,
        state: it.state,
        tone: it.tone,
        value: it.value,
        unit: it.unit,
        digits: it.digits,
      })),
    });
  }

  const usableLayers = layerOut.filter((l) => l.score !== null);
  const totalW = usableLayers.reduce((a, l) => a + l.weight, 0);
  const total = totalW > 0
    ? Math.round((usableLayers.reduce((a, l) => a + l.score * l.weight, 0) / totalW) * 10) / 10
    : null;

  let zone = null;
  if (total !== null) {
    for (const z of TEMP_ZONES) {
      if (z.lo <= total && total < z.hi) { zone = z; break; }
    }
  }

  const scores = layerOut.map((l) => l.score).filter((s) => s !== null);
  const spread = scores.length >= 2 ? Math.round((Math.max(...scores) - Math.min(...scores)) * 10) / 10 : null;

  return {
    value: total,
    state: zone ? zone.state : "数据不足",
    tone: zone ? zone.tone : "slate",
    zones: TEMP_ZONES,
    layers: layerOut,
    spread,
    formula: "综合温度 = 估值温度 ×50% + 情绪温度 ×30% + 资金温度 ×20%",
    coverage: Math.round((layerOut.filter((l) => l.score !== null).length / layerOut.length) * 100),
  };
}

function fmtReading(item) {
  const value = item.value;
  if (value === null || value === undefined) return "—";
  const digits = item.digits || 1;
  let text;
  if (Math.abs(value) >= 1000) {
    text = value.toLocaleString("en-US", { maximumFractionDigits: 0 });
  } else {
    text = value.toFixed(digits);
  }
  return text + (item.unit || "");
}

function pctText(pct, n = 0) {
  if (pct === null || pct === undefined) return `样本不足（${n}/${GREED_SAMPLE_DAYS}）`;
  return pct.toFixed(1);
}

function componentContext(key, detail) {
  if (key === "breadth") {
    const up = detail["上涨家数"];
    const down = detail["下跌家数"];
    const flat = detail["平盘"];
    if (up !== undefined && down !== undefined) {
      let text = `${up}涨/${down}跌`;
      if (flat !== undefined) text += `/${flat}平`;
      return text;
    }
  }
  if (key === "turnover_amt") {
    const chg = detail["较前一日%"];
    if (chg !== undefined && chg !== null) {
      const f = Number(chg);
      return Number.isFinite(f) ? `较前一日 ${f >= 0 ? "+" : ""}${f.toFixed(2)}%` : `较前一日 ${chg}`;
    }
  }
  return "";
}

function buildGreed(items, seriesMap) {
  seriesMap = seriesMap || {};
  const detailSkip = ["数据源", "分位窗口", "分位", "样本期数", "分位来源"];
  const components = [];
  for (const spec of GREED_COMPONENTS) {
    const key = spec.key;
    const item = items[key] || {};
    const value = item.value;
    const series = seriesMap[key] || [];
    const [pct, n] = samplePercentile(value, series, 1000000, GREED_MIN_SAMPLE);
    const isSent = spec.category === "sentiment";
    const weight = isSent ? GREED_SENTIMENT_WEIGHT / 3 : GREED_VALUATION_WEIGHT;
    const score = pct;
    const contribution = score !== null ? Math.round(score * weight * 100) / 100 : null;
    const zone = greedZoneOf(score);
    components.push({
      key,
      name: spec.name,
      category: spec.category,
      weight: Math.round(weight * 10000) / 10000,
      weight_pct: Math.round(weight * 1000) / 10,
      value,
      reading: fmtReading(item),
      context: componentContext(key, item.detail || {}),
      unit: item.unit || "",
      digits: item.digits || 1,
      as_of: item.as_of || "—",
      percentile: pct,
      sample_n: n,
      score,
      contribution,
      state: zone ? zone.state : "样本不足",
      tone: zone ? zone.tone : "slate",
      color: zone ? zone.color : "#9ca3af",
      detail_text: detailPairs(item.detail || {}, detailSkip).join(" · "),
    });
  }

  const sentimentComps = components.filter((c) => c.category === "sentiment");
  const valuationComp = components.find((c) => c.category === "valuation");
  const sentScores = sentimentComps.map((c) => c.score).filter((s) => s !== null);
  const sent = sentScores.length ? Math.round((sentScores.reduce((a, b) => a + b, 0) / sentScores.length) * 10) / 10 : null;
  const val = valuationComp ? valuationComp.score : null;

  const weighted = [];
  if (sent !== null) weighted.push([sent, GREED_SENTIMENT_WEIGHT]);
  if (val !== null) weighted.push([val, GREED_VALUATION_WEIGHT]);
  const total = weighted.length
    ? Math.round((weighted.reduce((a, [s, w]) => a + s * w, 0) / weighted.reduce((a, [, w]) => a + w, 0)) * 10) / 10
    : null;

  const totalW = weighted.reduce((a, [, w]) => a + w, 0) || 1.0;
  const effSent = sentScores.length ? GREED_SENTIMENT_WEIGHT / totalW / sentScores.length : 0;
  const effVal = val !== null ? GREED_VALUATION_WEIGHT / totalW : 0;
  for (const c of components) {
    c.excluded = false;
    if (c.category === "sentiment") {
      if (c.score === null) {
        c.weight = 0; c.weight_pct = 0; c.contribution = null; c.excluded = true;
      } else {
        c.weight = Math.round(effSent * 10000) / 10000;
        c.weight_pct = Math.round(effSent * 1000) / 10;
        c.contribution = Math.round(c.score * effSent * 100) / 100;
      }
    } else {
      c.weight = Math.round(effVal * 10000) / 10000;
      c.weight_pct = Math.round(effVal * 1000) / 10;
      c.contribution = c.score !== null ? Math.round(c.score * effVal * 100) / 100 : null;
      c.excluded = c.score === null;
    }
  }

  const zone = greedZoneOf(total);
  const zoneIndex = zone ? GREED_ZONES.indexOf(zone) : -1;

  const confirm = [];
  for (const key of GREED_CONFIRM_KEYS) {
    const item = items[key] || {};
    const [pct, n] = samplePercentile(item.value, seriesMap[key] || [], 1000000, 60);
    const name = key === "erp" ? "ERP 分位" : "破净率分位";
    const rule = key === "erp"
      ? "贪婪 < 20 且（ERP 分位 > 80 或 破净率分位 > 80）"
      : "泡沫警示仅使用 ERP 分位（< 20）";
    confirm.push({
      key,
      name,
      reading: fmtReading(item),
      value: item.value,
      unit: item.unit || "",
      as_of: item.as_of || "—",
      percentile: pct,
      sample_n: n,
      side: "冷侧分位",
      window: `全历史（${n} 期）`,
      rule,
      detail_text: detailPairs(item.detail || {}, detailSkip).join(" · "),
    });
  }

  const erpPct = (confirm.find((c) => c.key === "erp") || {}).percentile;
  const belowPct = (confirm.find((c) => c.key === "below_na") || {}).percentile;
  const erpN = (confirm.find((c) => c.key === "erp") || {}).sample_n || 0;
  const belowN = (confirm.find((c) => c.key === "below_na") || {}).sample_n || 0;
  const deepFear = !!(total !== null && total < 20 && ((erpPct !== null && erpPct > 80) || (belowPct !== null && belowPct > 80)));
  const bubble = !!(total !== null && total > 80 && erpPct !== null && erpPct < 20);

  let deepText, bubbleText;
  if (deepFear) {
    deepText = "已触发：冷 + 便宜共振，底部信号可信度最高";
  } else {
    deepText = `未触发（贪婪 ${pctText(total)}，ERP 分位 ${pctText(erpPct, erpN)}，破净率分位 ${pctText(belowPct, belowN)}）`;
  }
  if (bubble) {
    bubbleText = "已触发：热 + 贵共振，警惕均值回归";
  } else {
    bubbleText = `未触发（需贪婪 > 80 且 ERP 分位 < 20，当前贪婪 ${pctText(total)} / ERP 分位 ${pctText(erpPct, erpN)}）`;
  }

  const breadth = components.find((c) => c.key === "breadth");
  const rate = components.find((c) => c.key === "turnover_rate");
  let distortion = null;
  if (breadth && rate && breadth.score !== null && rate.score !== null && breadth.score < 20 && rate.score - breadth.score >= 30) {
    distortion = "放量杀跌使成交/换手分位抬升，贪婪读数可能高估真实情绪；宽度分位极低时，不可把读数误读为情绪回暖。";
  }

  function one(c) {
    if (c.score === null) return `${c.name} ${c.reading}（样本不足，仅 ${c.sample_n} 期）`;
    return `${c.name} ${c.reading}（分位 ${c.score.toFixed(1)}）`;
  }
  const sentTxt = sentimentComps.map(one).join(" / ");
  const valTxt = valuationComp ? one(valuationComp) : "沪深300 PE-TTM —";
  let expr = "";
  if (sentScores.length) expr = `(${sentScores.map((s) => s.toFixed(1)).join("+")})/${sentScores.length}×0.88`;
  if (val !== null) expr = (expr ? expr + " + " : "") + `${val.toFixed(1)}×0.12`;
  let summary = `市场贪婪指数 ${pctText(total)}（情绪×88% + 估值×12%，全历史分位）：情绪——${sentTxt}；估值——${valTxt}。`;
  if (expr) summary += `加权 = ${expr} = ${pctText(total)}。`;
  if (sentScores.length < sentimentComps.length) summary += `（市场宽度样本不足，情绪按可用 ${sentScores.length} 项重算。）`;
  if (distortion) summary += "【读数失真】" + distortion;
  summary += `确认层（全历史分位）：ERP 冷侧分位 ${pctText(erpPct, erpN)}、破净率冷侧分位 ${pctText(belowPct, belowN)}。深度恐慌闸门${deepText}；泡沫警示闸门${bubbleText}。`;

  return {
    value: total,
    state: zone ? zone.state : "数据不足",
    tone: zone ? zone.tone : "slate",
    color: zone ? zone.color : "#9ca3af",
    zone_index: zoneIndex,
    zones: GREED_ZONES,
    formula: "贪婪指数 = 情绪 × 88%（市场宽度 / 全A换手率 / 两市成交额 各 1/3）+ 估值 × 12%（沪深300 PE-TTM）",
    sentiment: {
      score: sent,
      weight: GREED_SENTIMENT_WEIGHT,
      covered: sentScores.length,
      total: sentimentComps.length,
    },
    valuation: { score: val, weight: GREED_VALUATION_WEIGHT },
    components,
    confirm,
    distortion,
    gates: {
      deep_fear: { name: "深度恐慌", triggered: deepFear, rule: "贪婪 < 20 且（ERP 分位 > 80 或 破净率分位 > 80）", text: deepText },
      bubble: { name: "泡沫警示", triggered: bubble, rule: "贪婪 > 80 且 ERP 分位 < 20", text: bubbleText },
    },
    summary,
    updated_note: "每日收盘后更新",
  };
}

function buildResponse(raw, seriesMap) {
  const items = {};
  for (const ind of INDICATORS) items[ind.key] = buildItem(ind, (raw || {})[ind.key]);

  const groups = [];
  for (const gkey of [GROUP_VALUATION, GROUP_SENTIMENT, GROUP_CAPITAL, GROUP_STRUCTURE]) {
    const members = INDICATOR_KEYS.filter((k) => INDICATOR_MAP[k].group === gkey).map((k) => items[k]);
    if (!members.length) continue;
    groups.push({
      key: gkey,
      title: GROUP_TITLES[gkey],
      subtitle: GROUP_SUBTITLES[gkey],
      tone: GROUP_TONES[gkey],
      items: members,
    });
  }

  const thermo = buildThermometer(items);
  const greed = buildGreed(items, seriesMap);

  return {
    status: "ok",
    tones: TONES,
    groups,
    thermometer: thermo,
    greed,
    indicators: items,
  };
}

function heatScoreOf(recValue, detail, ind) {
  let pct = detail["分位"];
  if (pct !== undefined && pct !== null) {
    const f = Number(pct);
    pct = Number.isFinite(f) ? f : null;
  } else {
    pct = null;
  }
  return scoreOf(ind, recValue, pct, detail);
}

// ================================================================ 温度计历史重建
function toMonthEnd(series) {
  const bag = {};
  for (const p of series || []) {
    if (p.value === null || p.value === undefined || !p.as_of) continue;
    bag[String(p.as_of).slice(0, 7)] = { as_of: p.as_of, value: p.value };
  }
  return Object.keys(bag).sort().map((k) => bag[k]);
}

function rollingPctSeries(series) {
  const pts = toMonthEnd(series);
  if (pts.length < 12) return [];
  const vals = pts.map((p) => p.value);
  const cap = PCT_WINDOW_POINTS.M;
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const windowed = vals.slice(Math.max(0, i + 1 - cap), i + 1);
    if (windowed.length < 12) continue;
    const below = windowed.filter((v) => v <= vals[i]).length;
    out.push({ as_of: pts[i].as_of, value: Math.round((below / windowed.length) * 1000) / 10 });
  }
  return out;
}

function reconstructTemperature(seriesMap) {
  const valuesCache = {};
  const pctCache = {};
  for (const ind of INDICATORS) {
    if (!ind.temp_weight || ind.temp_reconstruct === false) continue;
    const rows = toMonthEnd(seriesMap[ind.key] || []);
    if (!rows.length) continue;
    valuesCache[ind.key] = rows;
    const pctRows = rollingPctSeries(rows);
    if (pctRows.length) pctCache[ind.key] = pctRows;
  }

  if (!Object.keys(valuesCache).length) {
    return { monthly: [], note: "历史序列尚未积累，无法重建温度轨迹" };
  }

  const monthsSet = new Set();
  for (const rows of Object.values(valuesCache)) {
    for (const p of rows) monthsSet.add(String(p.as_of).slice(0, 7));
  }
  const months = [...monthsSet].sort();

  function latest(cache, key, month) {
    const rows = cache[key];
    if (!rows) return null;
    let hit = null;
    for (const p of rows) {
      if (String(p.as_of).slice(0, 7) <= month) hit = p;
      else break;
    }
    return hit;
  }

  const monthly = [];
  for (const month of months) {
    const layers = {};
    for (const layer of TEMP_LAYERS) {
      const members = INDICATORS.filter((i) => i.temp_layer === layer.key && i.temp_weight);
      let acc = 0;
      let wacc = 0;
      for (const ind of members) {
        const vp = latest(valuesCache, ind.key, month);
        if (!vp) continue;
        const pp = latest(pctCache, ind.key, month);
        const score = scoreFrom(ind, vp.value, pp ? pp.value : null);
        if (score === null) continue;
        acc += score * ind.temp_weight;
        wacc += ind.temp_weight;
      }
      layers[layer.key] = wacc > 0 ? Math.round((acc / wacc) * 10) / 10 : null;
    }

    let total = null;
    if (TEMP_LAYERS.every((l) => layers[l.key] !== null && layers[l.key] !== undefined)) {
      total = Math.round(TEMP_LAYERS.reduce((a, l) => a + layers[l.key] * l.weight, 0) * 10) / 10;
    }
    monthly.push({
      as_of: month + "-01",
      valuation: layers[GROUP_VALUATION],
      sentiment: layers[GROUP_SENTIMENT],
      capital: layers[GROUP_CAPITAL],
      total,
    });
  }

  return {
    monthly,
    note: "分位为逐点滚动口径（每点只用当时可见的历史）；分位样本不足 12 期的指标退回按读数档位打分，并按该月可得数据标注覆盖。三层齐备才给综合温度：日频情绪指标的历史由本页逐日积累，所以综合温度线会从「三个层都有数据」的月份才开始出现。",
  };
}

module.exports = {
  GROUP_VALUATION, GROUP_SENTIMENT, GROUP_CAPITAL, GROUP_STRUCTURE,
  TONES, SOURCE_LABELS, TEMP_LAYERS, TEMP_ZONES,
  GREED_SAMPLE_DAYS, GREED_MIN_SAMPLE, GREED_SENTIMENT_WEIGHT, GREED_VALUATION_WEIGHT,
  GREED_COMPONENTS, GREED_KEYS, GREED_CONFIRM_KEYS, GREED_ZONES,
  PCT_WINDOW_POINTS, FREQ_LABELS, CHART_KEYS,
  INDICATORS, INDICATOR_MAP, INDICATOR_KEYS, AUTO_KEYS, TEMP_KEYS,
  num, toDateStr, cleanSeries,
  percentileRank, bandOf, samplePercentile, greedZoneOf,
  dumpDetail, loadDetail,
  buildItem, buildThermometer, buildGreed, buildResponse, heatScoreOf,
  reconstructTemperature, scoreFrom, scoreOf, judge,
  fmtReading,
};
