"use strict";
/**
 * 宏观温度计：三个子温度计 → 加权合成 0-100 总分。
 * 忠实移植自 app/utils/macro_thermometer.py（口径由用户给定，打分锚点固定，保证历史可比）。
 * 某项缺数据时按「可用项权重归一化」处理，单指标失败不会让整根温度计作废。
 */

// 总分权重：资金面 25% / 实体热度 45% / 信用·部门 30%
const COMPOSITE_WEIGHTS = { liquidity: 0.25, heat: 0.45, structure: 0.3 };

const ZONE_COLORS = {
  ice: "#5B8FC7", cold: "#8BC8EA", neutral: "#E4D48F", warm: "#F4B393", hot: "#EA6668",
};

// 分数 → 冷热段位
const ZONE_BANDS = [[20, "ice"], [40, "cold"], [60, "neutral"], [80, "warm"], [101, "hot"]];
// 分数 → 中文状态标签
const STATE_BANDS = [[20, "低迷"], [40, "偏冷"], [60, "中性"], [80, "偏热"], [101, "亢奋 · 风险区"]];

// 锚点表：anchors 为 [[读数, 分数]] 升序，两点间线性插值；超界取端点分值
const SUB_INDICES = [
  {
    key: "liquidity",
    name: "① 资金面温度",
    weight: COMPOSITE_WEIGHTS.liquidity,
    direction: "松 = 高分（宽松是政策友好 / 未来热度的燃料）",
    source_note:
      "DR001 偏离与 R001−DR001 来自银行间回购定盘利率；1Y AAA 同业存单收益率来自中国货币网「收盘收益率曲线 → 同业存单(AAA)」（中债估值口径）。三项均为日频，打分统一用 5 日滚动均值降噪。",
    components: [
      { key: "dr001_spread", name: "DR001 − 政策利率（隔夜）", weight: 0.5, unit: "bp", digits: 0,
        anchors: [[-5, 100], [0, 90], [10, 72], [20, 52], [35, 28], [55, 8]],
        anchor_text: "≤−5bp→100 · 0→90 · +10→72 · +20→52 · +35→28 · ≥+55→8" },
      { key: "ncd_1y_aaa", name: "1Y AAA 同业存单", weight: 0.3, unit: "%", digits: 3,
        anchors: [[1.0, 100], [1.3, 80], [1.6, 60], [1.95, 30], [2.3, 0]],
        anchor_text: "≤1.00%→100 · 1.30→80 · 1.60→60 · 1.95→30 · ≥2.30→0" },
      { key: "r001_dr001", name: "R001 − DR001", weight: 0.2, unit: "bp", digits: 0,
        anchors: [[10, 90], [20, 70], [40, 40], [60, 10]],
        anchor_text: "≤10bp→90 · 20→70 · 40→40 · ≥60→10" },
    ],
  },
  {
    key: "heat",
    name: "② 实体热度温度",
    weight: COMPOSITE_WEIGHTS.heat,
    direction: "越高越热（经济跑不跑得动）",
    source_note: "PMI / PPI / M1、M2 同比来自东方财富数据中心（国家统计局口径），社融存量同比来自央行《金融统计数据报告》。",
    components: [
      { key: "pmi", name: "制造业 PMI", weight: 0.3, unit: "", digits: 1,
        anchors: [[45, 0], [48, 40], [50, 55], [52, 75], [55, 100]],
        anchor_text: "<45→0 · 48→40 · 50→55 · 52→75 · ≥55→100" },
      { key: "ppi_yoy", name: "PPI 同比", weight: 0.25, unit: "%", digits: 1,
        anchors: [[-2, 15], [0, 40], [3, 60], [6, 80], [9, 100]],
        anchor_text: "−2→15 · 0→40 · +3→60 · +6→80 · ≥+9→100" },
      { key: "tsf_yoy", name: "社融存量同比", weight: 0.25, unit: "%", digits: 1,
        anchors: [[6, 0], [7, 25], [8, 45], [10, 65], [12, 85], [14, 100]],
        anchor_text: "<6→0 · 7→25 · 8→45 · 10→65 · 12→85 · ≥14→100" },
      { key: "m1_m2", name: "M1 − M2 剪刀差", weight: 0.2, unit: "pp", digits: 1,
        anchors: [[-8, 0], [-5, 20], [-3, 40], [0, 60], [3, 80], [6, 100]],
        anchor_text: "<−8→0 · −5→20 · −3→40 · 0→60 · +3→80 · ≥+6→100" },
    ],
  },
  {
    key: "structure",
    name: "③ 信用 / 部门温度",
    weight: COMPOSITE_WEIGHTS.structure,
    direction: "资金活化 = 高分，空转 = 低分",
    source_note:
      "三项全部来自央行《金融统计数据报告》的分部门存贷款：企业与住户中长贷「同比多增」= 当月增量（相邻两期累计相减）− 去年同月增量；财政存款为当月增量。",
    components: [
      { key: "corp_ml_yoy", name: "企业中长贷 同比多增", weight: 0.4, unit: "亿", digits: 0,
        anchors: [[-4000, 0], [-2000, 25], [0, 50], [2000, 75], [4000, 100]],
        anchor_text: "少增≥4000→0 · −2000→25 · 0→50 · +2000→75 · 多增≥4000→100" },
      { key: "household_ml_yoy", name: "住户中长贷 同比多增", weight: 0.3, unit: "亿", digits: 0,
        anchors: [[-3000, 0], [-1500, 25], [0, 50], [1500, 75], [3000, 100]],
        anchor_text: "少增≥3000→0 · −1500→25 · 0→50 · +1500→75 · 多增≥3000→100" },
      { key: "fiscal_deposit_mom", name: "财政存款 当月增量", weight: 0.3, unit: "亿", digits: 0,
        anchors: [[-3000, 100], [-1500, 75], [0, 50], [1500, 25], [3000, 0]],
        anchor_text: "减少≥3000→100 · −1500→75 · 0→50 · +1500→25 · 增加≥3000→0" },
    ],
  },
];

const READING_BANDS = [
  { under: 40, label: "偏冷区", hint: "加配债、防御", tone: "cold" },
  { under: 60, label: "磨底区", hint: "结构行情，等信号", tone: "neutral" },
  { under: 75, label: "复苏确认区", hint: "跨过 60 就是升级信号", tone: "warm" },
  { under: 90, label: "偏热区", hint: "逐步降杠杆", tone: "hot" },
  { under: 101, label: "过热区", hint: "历史上对应 2015 / 2021 顶部，兑现", tone: "hot" },
];

const SPREAD_BANDS = [
  { under: 20, label: "传导顺畅", hint: "钱真正流到了企业和住户手里", tone: "neutral" },
  { under: 30, label: "温差收敛中", hint: "政策仍在暖、传导开始通", tone: "warm" },
  { under: 999, label: "空转加剧", hint: "政策暖但传导断，钱堆在资管", tone: "hot" },
];

const USAGE_STEPS = [
  "每月更新一次（金融数据发布后），三步走：① 取各项读数的最新值 → ② 按锚点表线性插值得单项分 → ③ 加权合成总分。",
];

const READING_NOTE =
  "判读阈值（比单看数字更重要）：<b>&lt;40</b> 偏冷区，加配债、防御；" +
  "<b>40-60</b> 磨底区，结构行情，等信号；" +
  "<b>≥60</b> 复苏确认区 —— 正好对应「反转三连」全部达标的合成分数" +
  "（算例：社融回 8.5%、企业中长贷多增 2000 亿、住户中长贷转正 → 实体 58 + 信用 53 → 综合 ≈ 60.5，" +
  "<b>跨过 60 就是升级信号</b>）；<b>&gt;75</b> 偏热区，逐步降杠杆；" +
  "<b>&gt;90</b> 过热区，历史上对应 2015 / 2021 顶部，兑现。";

const SPREAD_NOTE =
  "三个子温度分开看，比总分更有信息量：典型的「剪刀差结构」是资金面暖、部门温度冰" +
  "（例如资金 74 vs 部门 25，温差 49 分），说明政策暖但传导断。" +
  "温差收窄到 20 分以内，才说明钱真正流到了企业和住户手里。" +
  "你可以把「温差」本身当第四个指标：<b>资金 − 部门 ≥ 30 → 空转加剧，≤ 20 → 传导顺畅</b>。";

const CAVEAT_NOTE =
  "这套锚点都是经验口径，你可以按自己的风险偏好调整权重" +
  "（比如更看重实体热度就把 45% 提到 55%），但<b>打分锚点建议固定</b>，否则历史不可比。";

function zoneOf(score) {
  if (score == null) return null;
  for (const [upper, zone] of ZONE_BANDS) if (score < upper) return zone;
  return "hot";
}

function lerpScore(value, anchors) {
  if (value == null || !anchors || !anchors.length) return null;
  const v = Number(value);
  if (!Number.isFinite(v)) return null;
  const pts = anchors.slice().sort((a, b) => a[0] - b[0]);
  if (v <= pts[0][0]) return Number(pts[0][1]);
  if (v >= pts[pts.length - 1][0]) return Number(pts[pts.length - 1][1]);
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    if (x0 <= v && v <= x1) {
      if (x1 === x0) return Number(y1);
      return y0 + ((v - x0) / (x1 - x0)) * (y1 - y0);
    }
  }
  return Number(pts[pts.length - 1][1]);
}

function stateOf(score) {
  if (score == null) return null;
  for (const [upper, label] of STATE_BANDS) if (score < upper) return label;
  return STATE_BANDS[STATE_BANDS.length - 1][1];
}

function collectMetrics(items) {
  const metrics = {};
  const meta = {};
  for (const spec of SUB_INDICES) {
    for (const comp of spec.components) {
      const item = items[comp.key] || {};
      metrics[comp.key] = item.value;
      if (item.as_of) meta[comp.key] = String(item.as_of);
    }
  }
  return { metrics, meta };
}

function creditDetail(items) {
  const detail = (items.corp_ml_yoy && items.corp_ml_yoy.detail) || {};
  const keep = ["报告", "数据期", "企业中长期贷款", "住户中长期贷款", "财政性存款", "口径", "来源"];
  const out = {};
  for (const k of keep) if (detail[k] != null) out[k] = detail[k];
  return out;
}

function subIndex(spec, metrics, meta) {
  const components = [];
  let usedWeight = 0, acc = 0;
  for (const comp of spec.components) {
    const raw = metrics[comp.key];
    const score = lerpScore(raw, comp.anchors);
    if (score != null) { usedWeight += comp.weight; acc += score * comp.weight; }
    components.push({
      key: comp.key, name: comp.name, weight: comp.weight,
      weight_pct: Math.round(comp.weight * 100),
      unit: comp.unit, digits: comp.digits, raw,
      score: score == null ? null : Math.round(score * 10) / 10,
      state: stateOf(score), zone: zoneOf(score),
      as_of: meta[comp.key], anchor_text: comp.anchor_text,
      anchors: comp.anchors.map(([a, b]) => ({ value: a, score: b })),
    });
  }
  const score = usedWeight ? Math.round((acc / usedWeight) * 10) / 10 : null;
  return {
    key: spec.key, name: spec.name, weight: spec.weight,
    weight_pct: Math.round(spec.weight * 100),
    direction: spec.direction, source_note: spec.source_note,
    score, state: stateOf(score), zone: zoneOf(score),
    missing: components.filter((c) => c.score == null).map((c) => c.name),
    components,
  };
}

function readingOf(score) {
  if (score == null) return { label: "数据不足", hint: "关键指标缺失，无法合成总分", tone: "neutral" };
  for (const band of READING_BANDS) if (score < band.under) return { ...band };
  return { ...READING_BANDS[READING_BANDS.length - 1] };
}

function spreadOf(spread) {
  if (spread == null) return { label: "数据不足", hint: "资金面或部门温度缺失", tone: "neutral" };
  for (const band of SPREAD_BANDS) if (spread < band.under) return { ...band };
  return { ...SPREAD_BANDS[SPREAD_BANDS.length - 1] };
}

function computeThermometer(items) {
  const { metrics, meta } = collectMetrics(items);
  const subs = SUB_INDICES.map((s) => subIndex(s, metrics, meta));

  const available = {};
  for (const s of subs) if (s.score != null) available[s.key] = s.score;
  const usedWeight = Object.keys(available).reduce((a, k) => a + COMPOSITE_WEIGHTS[k], 0);
  const score = usedWeight
    ? Math.round((Object.entries(available).reduce((a, [k, v]) => a + v * COMPOSITE_WEIGHTS[k], 0) / usedWeight) * 10) / 10
    : null;

  const liq = available.liquidity, struc = available.structure;
  const spread = (liq != null && struc != null) ? Math.round((liq - struc) * 10) / 10 : null;

  const reading = readingOf(score);
  const spreadInfo = spreadOf(spread);

  return {
    score, zone: zoneOf(score),
    reading: reading.label, reading_hint: reading.hint, reading_tone: reading.tone,
    formula: "资金面温度 × 25% + 实体热度温度 × 45% + 信用/部门温度 × 30%",
    weights: subs.map((s) => ({
      key: s.key, name: s.name, weight: s.weight, weight_pct: s.weight_pct,
      score: s.score, state: s.state, zone: s.zone,
    })),
    subs,
    spread, spread_label: spreadInfo.label, spread_hint: spreadInfo.hint, spread_tone: spreadInfo.tone,
    spread_bands: SPREAD_BANDS.map((b) => ({ ...b })),
    reading_bands: READING_BANDS.map((b) => ({ ...b })),
    zone_colors: { ...ZONE_COLORS },
    zone_bands: ZONE_BANDS.map(([u, z]) => ({ under: u, zone: z })),
    derived_detail: creditDetail(items),
    missing: subs.filter((s) => s.score == null).map((s) => s.name),
    usage_steps: USAGE_STEPS.slice(),
    reading_note: READING_NOTE, spread_note: SPREAD_NOTE, caveat_note: CAVEAT_NOTE,
  };
}

module.exports = { computeThermometer, COMPOSITE_WEIGHTS, ZONE_COLORS };
