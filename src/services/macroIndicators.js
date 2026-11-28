"use strict";
/**
 * 宏观盯盘清单：三个维度 / 10 项指标的元数据、状态判定与组合解读。
 * 忠实移植自 app/utils/macro_indicators.py。
 *
 * 重要变更（Node 版）：
 *  - 移除 FALLBACK_DEFAULTS：拉不到的指标不写兜底值，卡片显示「数据缺失」（AC-12）。
 *  - 移除 akshare 导入：抓取逻辑移入 src/fetchers/，本模块只做元数据 + 纯计算。
 *  - 源标签增加 eastmoney（东方财富数据中心）。
 */
const { computeThermometer } = require("./macroThermometer");

// ---------------------------------------------------------------- 分组与色调
const GROUP_LIQUIDITY = "liquidity";
const GROUP_HEAT = "heat";
const GROUP_STRUCTURE = "structure";
const GROUP_ORDER = [GROUP_LIQUIDITY, GROUP_HEAT, GROUP_STRUCTURE];
const GROUP_TITLES = {
  [GROUP_LIQUIDITY]: "① 资金面（松 ↔ 紧）",
  [GROUP_HEAT]: "② 经济热度（冷 ↔ 热）",
  [GROUP_STRUCTURE]: "③ 部门结构（钱的流向）",
};
const GROUP_FREQS = {
  [GROUP_LIQUIDITY]: "日度",
  [GROUP_HEAT]: "月度",
  [GROUP_STRUCTURE]: "月度 · 累计口径",
};
const GROUP_DESCS = {
  [GROUP_LIQUIDITY]: "钱贵不贵：看「价」（DR001 偏离、1Y 同业存单）与「分层」（R001−DR001）",
  [GROUP_HEAT]: "经济跑不跑：看「量」（社融）与「温度」（PMI、PPI、M1−M2）",
  [GROUP_STRUCTURE]: "钱在谁手里：看企业中长贷、住户中长贷与财政存款，判断信用扩张与资金回笼",
};
const TONES = {
  blue: "#3b82f6", cyan: "#06b6d4", green: "#22c55e", lime: "#84cc16",
  amber: "#f59e0b", orange: "#f97316", red: "#ef4444",
};

// ---------------------------------------------------------------- 指标清单
const INDICATORS = [
  {
    key: "dr001_spread", group: GROUP_LIQUIDITY, name: "DR001 − 政策利率（隔夜）",
    subtitle: "隔夜资金价格 vs 央行政策利率锚", unit: "bp", digits: 0, source: "auto",
    meaning:
      "DR001 是存款类机构（银行）隔夜期质押式回购加权利率；逆回购操作利率是央行公开市场操作的锚（随公告更新）。两者之差即银行隔夜拿钱的成本相对政策利率偏离了多少，正值表示银行体系隔夜资金偏紧。",
    guide:
      "看「价」。负偏离 = 隔夜资金比政策利率还便宜，说明钱多；正偏离持续扩大 = 银行体系缺钱，缴税、政府债缴款高峰时常出现；+35bp 以上属明显紧张。",
    bands: [
      { range: "≤ −5bp", state: "很松，钱富余", tone: "blue" },
      { range: "−5 ~ 0bp", state: "偏松", tone: "cyan" },
      { range: "0 ~ +10bp", state: "平稳", tone: "green" },
      { range: "+10 ~ +20bp", state: "略偏紧", tone: "amber" },
      { range: "+20 ~ +35bp", state: "偏紧", tone: "orange" },
      { range: "≥ +55bp", state: "钱荒级别", tone: "red" },
    ],
  },
  {
    key: "ncd_1y_aaa", group: GROUP_LIQUIDITY, name: "1Y AAA 同业存单",
    subtitle: "银行负债成本（中国货币网收盘收益率曲线）", unit: "%", digits: 3, source: "auto",
    meaning:
      "1 年期 AAA 同业存单到期收益率，来自中国货币网「收盘收益率曲线 → 同业存单(AAA)」（中债估值口径）。它是银行主动负债的边际成本，也是资金面「水位」的价格化体现。",
    guide: "看「水位」。收益率低位 = 银行负债压力小、资金宽裕；快速上行 = 负债端收紧，往往先于信贷投放放缓出现。",
    bands: [
      { range: "≤ 1.00%", state: "极低，资金宽松", tone: "blue" },
      { range: "1.00 ~ 1.30%", state: "低位", tone: "cyan" },
      { range: "1.30 ~ 1.60%", state: "中性", tone: "green" },
      { range: "1.60 ~ 1.95%", state: "偏高，负债成本抬升", tone: "amber" },
      { range: "≥ 2.30%", state: "高位，融资成本高", tone: "red" },
    ],
  },
  {
    key: "r001_dr001", group: GROUP_LIQUIDITY, name: "R001 − DR001",
    subtitle: "非银分层", unit: "bp", digits: 0, source: "auto",
    meaning:
      "R001 是银行间全市场（含非银机构）隔夜质押式回购加权利率，DR001 是存款类机构（银行）隔夜回购加权利率。二者之差衡量\"非银分层\"——非银机构借钱比银行贵多少。",
    guide:
      "看分层。利差小 = 银行与非银融资成本接近，流动性传导顺畅；利差走阔 = 非银（基金、券商、理财）借钱变贵，常见于资金收紧、去杠杆或年末考核时点；>50bp 属极端（2020 年 12 月曾冲上百 bp）。",
    bands: [
      { range: "< 15bp", state: "正常", note: "流动性传导顺畅", tone: "green" },
      { range: "15 ~ 30bp", state: "分层走阔", note: "非银融资成本抬升", tone: "amber" },
      { range: "30 ~ 50bp", state: "结构性紧张，非银借钱变贵", note: "非银明显更贵", tone: "orange" },
      { range: "> 50bp", state: "极端", note: "2020 年 12 月曾冲上百 bp", tone: "red" },
    ],
  },
  {
    key: "pmi", group: GROUP_HEAT, name: "制造业 PMI",
    subtitle: "经济景气领先指标", unit: "", digits: 1, source: "auto",
    meaning: "制造业采购经理指数，国家统计局按月发布（次月初公布），是经济景气的领先指标。50 为荣枯线：>50 扩张、<50 收缩。",
    guide: "看\"温度\"中公布最早的月度数据。48~50 = 荣枯线附近磨底 / 弱复苏；连续高于 50 且上行 = 复苏确认；跌破 48 需警惕。",
    bands: [
      { range: "≥ 52", state: "强劲扩张", tone: "red" },
      { range: "50 ~ 52", state: "温和扩张", tone: "amber" },
      { range: "48 ~ 50", state: "荣枯线附近磨底", note: "弱复苏", tone: "green" },
      { range: "45 ~ 48", state: "明显收缩", tone: "cyan" },
      { range: "< 45", state: "深度收缩", tone: "blue" },
    ],
  },
  {
    key: "tsf_yoy", group: GROUP_HEAT, name: "社融存量同比",
    subtitle: "宽信用 / 信用收缩", unit: "%", digits: 1, source: "auto",
    meaning:
      "社会融资规模存量同比，衡量实体经济从金融体系获得的资金总量增速，是\"宽信用\"最直接的度量。由央行按月发布。",
    guide: "看\"量\"。社融存量同比上行 = 信用扩张、实体融资需求改善；下行 = 信用收缩，\"宽货币\"没进实体。是判断\"资金松 + 信用紧\"组合的关键。",
    bands: [
      { range: "> 12%", state: "宽信用 / 过热（2020 年曾 13.3%，随后政策转向）", tone: "red" },
      { range: "10 ~ 12%", state: "偏宽", tone: "amber" },
      { range: "8 ~ 10%", state: "中性（2018–2021 常态）", tone: "green" },
      { range: "< 8%", state: "信用收缩", tone: "blue" },
    ],
  },
  {
    key: "m1_m2", group: GROUP_HEAT, name: "M1 − M2 剪刀差",
    subtitle: "资金活化程度", unit: "pp", digits: 1, source: "auto",
    meaning:
      "M1 同比 减去 M2 同比。M1（狭义货币）以企业活期存款和现金为主，代表\"活钱\"、交易性需求；M2（广义货币）含定期存款，代表\"沉淀的钱\"。剪刀差为正说明资金在活化、企业和居民更愿意交易投资。",
    guide:
      "剪刀差回升（负值收窄）= 资金活化、经济热度上行，通常领先于企业与权益市场预期改善；持续为负且扩大 = 资金淤积、需求偏弱。",
    bands: [
      { range: "> 0", state: "资金活化强、经济热度高", note: "2016–17 牛市区间", tone: "red" },
      { range: "0 ~ −3%", state: "中性", tone: "green" },
      { range: "−3 ~ −6%", state: "偏冷", tone: "cyan" },
      { range: "< −6%", state: "深度偏冷", note: "2024 年曾到约 −8%", tone: "blue" },
    ],
  },
  {
    key: "ppi_yoy", group: GROUP_HEAT, name: "PPI 同比",
    subtitle: "工业品价格与上游成本压力", unit: "%", digits: 1, source: "auto",
    meaning: "工业生产者出厂价格指数同比，衡量工业品出厂价格变化，反映工业供需状况与上游成本压力。国家统计局按月发布。",
    guide: "看工业品的\"体温\"。PPI 转正且温和上行 = 供需改善、企业盈利修复；>5% 需警惕成本向中下游挤压；持续深度为负 = 工业通缩。",
    bands: [
      { range: "> 5%", state: "过热，成本挤压中下游", tone: "red" },
      { range: "2 ~ 5%", state: "偏热、供需两旺", tone: "amber" },
      { range: "0 ~ 2%", state: "温和回升", tone: "lime" },
      { range: "−2 ~ 0%", state: "通缩阴影", tone: "cyan" },
      { range: "−3 ~ −2%", state: "偏冷", tone: "cyan" },
      { range: "< −3%", state: "深度通缩（2023–2024 常态）", tone: "blue" },
    ],
  },
  {
    key: "corp_ml_yoy", group: GROUP_STRUCTURE, name: "企业中长贷 同比多增",
    subtitle: "企业资本开支意愿（当月同比）", unit: "亿", digits: 0, source: "auto",
    meaning:
      "企（事）业单位中长期贷款当月增量相对去年同月的多增额。央行《金融统计数据报告》只披露年初至今累计数，先用相邻两期累计相减得到当月增量，再与去年同月比较。中长贷对应企业的资本开支与设备投资，是信用扩张「实不实」的核心观察。",
    guide: "同比多增 = 企业愿意借钱做长期投资；持续少增 = 企业收缩资本开支。短贷与票据冲量不算资本开支，本页不纳入打分。",
    bands: [
      { range: "多增 ≥ 4000 亿", state: "企业扩张明显", tone: "red" },
      { range: "多增 2000 ~ 4000 亿", state: "扩张意愿回升", tone: "amber" },
      { range: "0 ~ 2000 亿", state: "小幅多增", tone: "green" },
      { range: "少增 0 ~ 2000 亿", state: "边际收缩", tone: "cyan" },
      { range: "少增 ≥ 2000 亿", state: "明显收缩", tone: "blue" },
    ],
  },
  {
    key: "household_ml_yoy", group: GROUP_STRUCTURE, name: "住户中长贷 同比多增",
    subtitle: "居民按揭与长期消费（当月同比）", unit: "亿", digits: 0, source: "auto",
    meaning: "住户中长期贷款当月增量相对去年同月的多增额，口径同上（累计相减 + 同比）。住户中长贷以按揭为主，直接反映居民加杠杆与地产销售意愿。",
    guide: "同比多增 = 居民重新加杠杆，通常对应地产销售与消费信心修复；持续少增 = 居民提前还贷、缩表。",
    bands: [
      { range: "多增 ≥ 3000 亿", state: "居民扩张明显", tone: "red" },
      { range: "多增 1500 ~ 3000 亿", state: "加杠杆意愿回升", tone: "amber" },
      { range: "0 ~ 1500 亿", state: "小幅多增", tone: "green" },
      { range: "少增 0 ~ 1500 亿", state: "边际收缩", tone: "cyan" },
      { range: "少增 ≥ 1500 亿", state: "居民缩表", tone: "blue" },
    ],
  },
  {
    key: "fiscal_deposit_mom", group: GROUP_STRUCTURE, name: "财政存款 当月增量",
    subtitle: "财政投放还是回笼", unit: "亿", digits: 0, source: "auto",
    meaning:
      "财政性存款当月增量（相邻两期累计相减）。财政存款增加 = 税款/债券资金进入国库、从银行体系抽走流动性；减少 = 财政支出投放流动性。",
    guide: "减少（尤其 ≥3000 亿）= 财政投放，利好流动性；增加（尤其 ≥3000 亿）= 财政回笼，抽走银行体系资金，常出现在缴税大月与政府债发行高峰。",
    bands: [
      { range: "减少 ≥ 3000 亿", state: "财政大幅投放", tone: "green" },
      { range: "减少 0 ~ 3000 亿", state: "财政小幅投放", tone: "lime" },
      { range: "增加 0 ~ 3000 亿", state: "财政小幅回笼", tone: "amber" },
      { range: "增加 ≥ 3000 亿", state: "财政明显回笼", tone: "red" },
    ],
  },
];

const INDICATOR_MAP = Object.fromEntries(INDICATORS.map((i) => [i.key, i]));
const INDICATOR_KEYS = INDICATORS.map((i) => i.key);

// 图表专用额外序列（不作为卡片展示，只存历史表供趋势图）
const SERIES_EXTRA_KEYS = { m1_yoy: "M1 同比", m2_yoy: "M2 同比" };

// 逆回购操作利率：央行政策利率锚，用于计算 DR001 偏离
const PARAM_REVERSE_REPO = "reverse_repo_rate";
const PARAM_META = {
  [PARAM_REVERSE_REPO]: {
    name: "逆回购操作利率（政策利率锚）", unit: "%", digits: 2,
    hint: "央行最新一期公开市场逆回购操作利率（随《公开市场业务交易公告》更新，期限可能为隔夜或 7 天期），用于计算 DR001 偏离。",
  },
};

const AK_KEYS = ["dr001_spread", "ncd_1y_aaa", "r001_dr001", "pmi", "m1_m2", "ppi_yoy"];
const PBC_KEYS = ["tsf_yoy", "corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom", PARAM_REVERSE_REPO];
const AUTO_KEYS = [...AK_KEYS, ...PBC_KEYS];
const AUTO_SOURCES = new Set(["akshare", "pbc", "eastmoney"]);

const SOURCE_LABELS = {
  akshare: "akshare", pbc: "央行", eastmoney: "东方财富", em: "东方财富",
};

const HISTORY_KEYS = new Set([...INDICATOR_KEYS, PARAM_REVERSE_REPO, ...Object.keys(SERIES_EXTRA_KEYS)]);

const NAME_OF_KEY = Object.fromEntries(INDICATORS.map((i) => [i.key, i.name]));
NAME_OF_KEY[PARAM_REVERSE_REPO] = PARAM_META[PARAM_REVERSE_REPO].name;
Object.assign(NAME_OF_KEY, SERIES_EXTRA_KEYS);

// ---------------------------------------------------------------- 状态判定
function decide(key, value) {
  if (value == null) return null;
  const v = Number(value);
  if (!Number.isFinite(v)) return null;

  if (key === "dr001_spread") {
    if (v <= -5) return ["很松，钱富余", "blue", 0];
    if (v < 0) return ["偏松", "cyan", 1];
    if (v < 10) return ["平稳", "green", 2];
    if (v < 20) return ["略偏紧", "amber", 3];
    if (v < 35) return ["偏紧", "orange", 4];
    if (v < 55) return ["明显紧张", "orange", 4];
    return ["钱荒级别", "red", 5];
  }
  if (key === "ncd_1y_aaa") {
    if (v <= 1.0) return ["极低，资金宽松", "blue", 0];
    if (v < 1.3) return ["低位，宽松", "cyan", 1];
    if (v < 1.6) return ["中性", "green", 2];
    if (v < 1.95) return ["偏高，负债成本抬升", "amber", 3];
    return ["高位，融资成本高", "red", 4];
  }
  if (key === "r001_dr001") {
    if (v < 15) return ["正常", "green", 2];
    if (v < 30) return ["分层走阔", "amber", 3];
    if (v < 50) return ["结构性紧张，非银借钱变贵", "orange", 4];
    return ["极端", "red", 5];
  }
  if (key === "pmi") {
    if (v >= 52) return ["强劲扩张", "red", 4];
    if (v >= 50) return ["温和扩张", "amber", 3];
    if (v >= 48) return ["荣枯线附近磨底", "green", 2];
    if (v >= 45) return ["明显收缩", "cyan", 1];
    return ["深度收缩", "blue", 0];
  }
  if (key === "tsf_yoy") {
    if (v > 12) return ["宽信用 / 过热", "red", 4];
    if (v >= 10) return ["偏宽", "amber", 3];
    if (v >= 8) return ["中性", "green", 2];
    return ["信用收缩", "blue", 0];
  }
  if (key === "m1_m2") {
    if (v > 0) return ["资金活化强、经济热度高", "red", 4];
    if (v >= -3) return ["中性", "green", 2];
    if (v >= -6) return ["偏冷", "cyan", 1];
    return ["深度偏冷", "blue", 0];
  }
  if (key === "ppi_yoy") {
    if (v > 5) return ["过热，成本挤压中下游", "red", 4];
    if (v >= 2) return ["偏热、供需两旺", "amber", 3];
    if (v >= 0) return ["温和回升", "lime", 2];
    if (v >= -2) return ["通缩阴影", "cyan", 1];
    if (v >= -3) return ["偏冷", "cyan", 1];
    return ["深度通缩", "blue", 0];
  }
  if (key === "corp_ml_yoy") {
    if (v >= 4000) return ["企业扩张明显", "red", 4];
    if (v >= 2000) return ["扩张意愿回升", "amber", 3];
    if (v >= 0) return ["小幅多增", "green", 2];
    if (v >= -2000) return ["边际收缩", "cyan", 1];
    return ["明显收缩", "blue", 0];
  }
  if (key === "household_ml_yoy") {
    if (v >= 3000) return ["居民扩张明显", "red", 4];
    if (v >= 1500) return ["加杠杆意愿回升", "amber", 3];
    if (v >= 0) return ["小幅多增", "green", 2];
    if (v >= -1500) return ["边际收缩", "cyan", 1];
    return ["居民缩表", "blue", 0];
  }
  if (key === "fiscal_deposit_mom") {
    if (v <= -3000) return ["财政大幅投放", "green", 4];
    if (v < 0) return ["财政小幅投放", "lime", 3];
    if (v < 3000) return ["财政小幅回笼", "amber", 1];
    return ["财政明显回笼", "red", 0];
  }
  return null;
}

// ---------------------------------------------------------------- 组合解读
const QUADRANTS = [
  { title: "资金松 + 信用紧", brief: "宽货币没进实体",
    desc: "钱堆在银行间和债市，对应\"资产荒\"、长债利率低位。DR001 平 ≠ 经济好，必须拿社融、PMI 证伪。", tone: "cyan" },
  { title: "资金松 + 信用松", brief: "全面宽松", desc: "货币与信用同向扩张，股、债、商品往往同涨。", tone: "green" },
  { title: "资金紧 + 热度高", brief: "政策收紧或通胀压制", desc: "流动性收紧叠加经济偏热，存在股债双杀风险。", tone: "orange" },
  { title: "资金紧 + 热度低", brief: "最差组合", desc: "衰退 + 流动性收缩，风险资产承压最明显。", tone: "red" },
];
const MEMORY_LINE = "钱贵不贵（资金面）→ 经济跑不跑（热度）→ 钱在谁手里（部门结构）。";
const WATCH_LIST = [
  { key: "tsf_yoy", label: "社融存量同比企稳（回到 8% 以上）" },
  { key: "household_ml_yoy", label: "住户中长贷同比转正（居民重新加杠杆）" },
  { key: "turnover_amt", label: "成交额与两融余额同向放大（情绪有杠杆支撑）" },
];

function avg(values) {
  const vals = values.filter((v) => v != null && Number.isFinite(Number(v))).map(Number);
  if (!vals.length) return null;
  return Math.round((vals.reduce((a, b) => a + b, 0) / vals.length) * 100) / 100;
}

function summaryText(liquidity, credit, structure, tsf) {
  const segs = [];
  if (liquidity.side === "松") segs.push("宽货币（资金面平稳）");
  else if (liquidity.side === "紧") segs.push("资金面收紧");
  else segs.push("货币中性");
  if (credit.side === "紧" && tsf != null) segs.push(`紧信用（社融存量同比 ${tsf}%）`);
  else if (credit.side === "松" && tsf != null) segs.push(`信用扩张（社融存量同比 ${tsf}%）`);
  else segs.push(credit.label || "信用待观察");
  segs.push(`部门结构：${structure.label || "待观察"}`);
  return segs.join(" + ") + " → 盯「社融企稳 + 住户贷款转正 + 成交额与两融同向」，三者共振才是全面反转信号。";
}

function computeCombination(items) {
  const liqAvg = avg(["dr001_spread", "ncd_1y_aaa", "r001_dr001"].map((k) => (items[k] || {}).level));
  const heatAvg = avg(["pmi", "tsf_yoy", "ppi_yoy"].map((k) => (items[k] || {}).level));
  const structAvg = avg(["corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"].map((k) => (items[k] || {}).level));

  let liquidity, liqSide;
  if (liqAvg == null) { liquidity = { label: "数据不足", tone: "green", reason: "资金面指标缺失" }; liqSide = null; }
  else if (liqAvg < 2.5) { liquidity = { label: "资金松", tone: "green", reason: "资金价格与水位均不紧" }; liqSide = "松"; }
  else if (liqAvg < 3.5) { liquidity = { label: "资金中性", tone: "amber", reason: "资金面接近均衡、边际有扰动" }; liqSide = "中性"; }
  else { liquidity = { label: "资金紧", tone: "red", reason: "资金价格偏高或水位偏低" }; liqSide = "紧"; }

  let heat, heatSide;
  if (heatAvg == null) { heat = { label: "数据不足", tone: "green", reason: "经济热度指标缺失" }; heatSide = null; }
  else if (heatAvg < 1.5) { heat = { label: "热度偏低", tone: "cyan", reason: "量价指标偏冷" }; heatSide = "冷"; }
  else if (heatAvg < 2.5) { heat = { label: "热度中性", tone: "green", reason: "冷热指标互有强弱" }; heatSide = "中性"; }
  else { heat = { label: "热度偏高", tone: "red", reason: "量价指标整体偏热" }; heatSide = "热"; }

  let structure;
  if (structAvg == null) structure = { label: "数据不足", tone: "green", reason: "部门结构指标缺失" };
  else if (structAvg < 1.5) structure = { label: "钱淤积（居民缩表）", tone: "blue", reason: "剪刀差深负、居民净还贷" };
  else if (structAvg < 2.5) structure = { label: "中性", tone: "green", reason: "活化与缩表信号互有强弱" };
  else structure = { label: "钱在活化", tone: "orange", reason: "剪刀差收敛、资金向金融市场迁移" };

  const tsf = (items.tsf_yoy || {}).value;
  let credit, creditSide;
  if (tsf == null) { credit = { label: "信用数据缺失", tone: "green", value: null }; creditSide = null; }
  else if (tsf < 8) { credit = { label: "信用收缩", tone: "blue", value: tsf }; creditSide = "紧"; }
  else if (tsf < 10) { credit = { label: "信用中性", tone: "green", value: tsf }; creditSide = "中性"; }
  else { credit = { label: "信用扩张", tone: "amber", value: tsf }; creditSide = "松"; }

  let activeTitle = null;
  if (liqSide === "松" && creditSide === "紧") activeTitle = "资金松 + 信用紧";
  else if (liqSide === "松" && creditSide === "松") activeTitle = "资金松 + 信用松";
  else if (liqSide === "紧" && heatSide === "热") activeTitle = "资金紧 + 热度高";
  else if (liqSide === "紧" && heatSide === "冷") activeTitle = "资金紧 + 热度低";

  const quadrants = QUADRANTS.map((q) => ({ ...q, active: q.title === activeTitle }));
  let quadrant;
  if (activeTitle) quadrant = { ...QUADRANTS.find((q) => q.title === activeTitle), active: true };
  else quadrant = {
    title: "混合状态", brief: "当前不落在四个典型象限",
    desc: "资金面与信用/热度信号不一致（或数据不足），建议以单指标读数与趋势变化为准，等信号收敛后再下判断。",
    tone: "green", active: true,
  };

  liquidity.side = liqSide; heat.side = heatSide; credit.side = creditSide;

  return {
    liquidity, heat, credit, structure, quadrant, quadrants,
    memory: MEMORY_LINE, summary: summaryText(liquidity, credit, structure, tsf),
    watch: WATCH_LIST, liquidity_avg: liqAvg, heat_avg: heatAvg, structure_avg: structAvg,
  };
}

// ---------------------------------------------------------------- 组装响应
function sourceNote(detail) {
  for (const f of ["报告期", "报告", "公告", "来源"]) {
    const v = detail && detail[f];
    if (v) return String(v);
  }
  return "";
}

function buildItems(raw) {
  const items = {};
  for (const ind of INDICATORS) {
    const row = raw[ind.key] || {};
    const value = row.value;
    const d = decide(ind.key, value);
    const [state, tone, level] = d || [null, null, null];
    const source = row.source || "";
    items[ind.key] = {
      key: ind.key, group: ind.group, name: ind.name, subtitle: ind.subtitle,
      unit: ind.unit, digits: ind.digits, source,
      source_label: SOURCE_LABELS[source] || source,
      source_detail: row.source_detail || "",
      value, as_of: row.as_of, detail: row.detail || {}, error: row.error || null,
      state, tone, level, meaning: ind.meaning, guide: ind.guide, bands: ind.bands,
    };
  }
  return items;
}

function buildResponse(raw, params) {
  const items = buildItems(raw);
  const groups = GROUP_ORDER.map((gk) => ({
    key: gk, title: GROUP_TITLES[gk], freq: GROUP_FREQS[gk], desc: GROUP_DESCS[gk],
    items: INDICATOR_KEYS.filter((k) => items[k].group === gk).map((k) => items[k]),
  }));
  return {
    status: "ok", groups, params,
    combination: computeCombination(items),
    thermometer: computeThermometer(items),
    tones: TONES,
  };
}

function dumpDetail(detail) {
  try { return JSON.stringify(detail || {}); } catch (_) { return "{}"; }
}
function loadDetail(text) {
  if (!text) return {};
  try { const d = JSON.parse(text); return d && typeof d === "object" ? d : {}; } catch (_) { return {}; }
}

module.exports = {
  GROUP_ORDER, GROUP_TITLES, GROUP_FREQS, GROUP_DESCS, TONES,
  INDICATORS, INDICATOR_MAP, INDICATOR_KEYS, SERIES_EXTRA_KEYS,
  PARAM_REVERSE_REPO, PARAM_META, AK_KEYS, PBC_KEYS, AUTO_KEYS, AUTO_SOURCES,
  SOURCE_LABELS, HISTORY_KEYS, NAME_OF_KEY,
  decide, buildItems, buildResponse, computeCombination, sourceNote,
  dumpDetail, loadDetail,
};
