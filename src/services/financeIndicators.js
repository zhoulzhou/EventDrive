"use strict";
/**
 * 财务指标图表计算：移植自 app/utils/finance_indicators.py。
 * records: 按报告期升序的对象数组，键为列名（revenue/operating_cost/...）+ report_date。
 *
 * 约定：
 *  - 利润表/现金流量表为累计值：单季值 = 本季累计 − 同自然年内上一季度累计（Q1 取累计；跨年/缺季置空）
 *  - 资产负债表为时点值：直接用期末值
 *  - 周转类按期初期末平均余额 + 累计口径年化
 *  - base 为 0/负时增速标记为 "—"/"扭亏"，不硬算百分比
 */
const { FINANCIAL_FIELDS } = require("../db/finance");

// [key, 名称, 展示单位, 增速类型 yoy/qoq]
const INDICATOR_DEFS = [
  ["revenue", "营业收入", "亿", "yoy"],
  ["operating_cost", "营业成本", "亿", "yoy"],
  ["gross_profit", "毛利", "亿", "yoy"],
  ["net_profit", "归母净利润", "亿", "yoy"],
  ["operating_cash_flow", "经营现金流净额", "亿", "yoy"],
  ["total_cash", "现金总额", "亿", "yoy"],
  ["inventory", "存货", "亿", "yoy"],
  ["accounts_receivable", "应收账款", "亿", "yoy"],
  ["contract_liabilities", "合同负债", "亿", "yoy"],
  ["short_term_borrowing", "短期借款", "亿", "qoq"],
  ["long_term_borrowing", "长期借款", "亿", "yoy"],
  ["interest_expense", "利息支出", "亿", "qoq"],
  ["gross_margin", "毛利率", "%", "yoy"],
  ["net_margin", "净利率", "%", "yoy"],
  ["inventory_turnover_days", "存货周转天数", "天", "yoy"],
  ["receivables_turnover_days", "应收账款周转天数", "天", "yoy"],
  ["interest_bearing_debt", "有息负债合计", "亿", "yoy"],
];

const INTEREST_DEBT_FIELDS = [
  "short_term_borrowing",
  "non_current_liab_due_1y",
  "long_term_borrowing",
  "bonds_payable",
];

const val = (rec, attr) => {
  if (!rec) return null;
  const v = rec[attr];
  return v == null ? null : Number(v);
};
const quarterNo = (d) => Math.floor((Number(d.slice(5, 7)) - 1) / 3) + 1;
const toYi = (yuan) => (yuan == null ? null : Math.round((yuan / 1e8) * 10) / 10);
const round1 = (v) => (v == null ? null : Math.round(v * 10) / 10);

function sameQuarterLastYear(records, i) {
  const d = records[i].report_date;
  const ty = String(Number(d.slice(0, 4)) - 1);
  const md = d.slice(5);
  for (let j = 0; j < records.length; j++) {
    if (records[j].report_date.slice(0, 4) === ty && records[j].report_date.slice(5) === md) return j;
  }
  return null;
}

function growthPct(cur, base) {
  if (cur == null || base == null) return [null, null];
  if (base > 0) return [round1(((cur - base) / base) * 100), null];
  if (base === 0) return [null, "—"];
  if (cur > 0) return [null, "扭亏"];
  return [null, "—"];
}

function growthMargin(cur, base) {
  if (cur == null || base == null) return [null, null];
  return [round1(cur - base), null];
}

function sumSeries(...lists) {
  const n = lists[0] ? lists[0].length : 0;
  const out = [];
  for (let i = 0; i < n; i++) {
    const nums = [];
    for (const l of lists) if (l[i] != null) nums.push(l[i]);
    out.push(nums.length ? nums.reduce((a, b) => a + b, 0) : null);
  }
  return out;
}

/**
 * @param {Array<object>} records 按 report_date 升序
 * @param {string|null} start 展示起始 YYYY-MM-DD
 * @param {string|null} end   展示截止 YYYY-MM-DD
 * @returns {{periods:string[], indicators:object[]}}
 */
function computeIndicators(records, start, end) {
  const n = records.length;
  const dates = records.map((r) => r.report_date);

  const singleSeries = (attr) => {
    const out = [];
    for (let i = 0; i < n; i++) {
      const cum = val(records[i], attr);
      if (cum == null) {
        out.push(null);
        continue;
      }
      if (quarterNo(dates[i]) === 1) {
        out.push(cum);
        continue;
      }
      let prev = null;
      if (i > 0) {
        const pd = dates[i - 1];
        if (pd.slice(0, 4) === dates[i].slice(0, 4) && quarterNo(pd) === quarterNo(dates[i]) - 1) {
          prev = val(records[i - 1], attr);
        }
      }
      out.push(prev == null ? null : cum - prev);
    }
    return out;
  };

  const revQ = singleSeries("revenue");
  const costQ = singleSeries("operating_cost");
  const npQ = singleSeries("net_profit");
  const ocfQ = singleSeries("operating_cash_flow");
  const ieQ = singleSeries("interest_expense");
  const grossQ = revQ.map((a, i) => (a != null && costQ[i] != null ? a - costQ[i] : null));

  const pointSeries = (attr) => records.map((r) => val(r, attr));
  const cashPt = sumSeries(pointSeries("cash"), pointSeries("short_term_investment"));
  const inventoryPt = pointSeries("inventory");
  const receivablesPt = pointSeries("accounts_receivable");
  const contractPt = pointSeries("contract_liabilities");
  const stBorrowPt = pointSeries("short_term_borrowing");
  const ltBorrowPt = pointSeries("long_term_borrowing");
  const ibDebtPt = [];
  for (let i = 0; i < n; i++) {
    const nums = [];
    for (const f of INTEREST_DEBT_FIELDS) {
      const v = val(records[i], f);
      if (v != null) nums.push(v);
    }
    ibDebtPt.push(nums.length ? nums.reduce((a, b) => a + b, 0) : null);
  }

  const ratioSeries = (numer, denom) =>
    numer.map((a, i) => {
      const b = denom[i];
      if (a == null || b == null || b === 0) return null;
      return (a / b) * 100;
    });
  const grossMargin = ratioSeries(grossQ, revQ);
  const netMargin = ratioSeries(npQ, revQ);

  const avgPrevCur = (series) => {
    const out = [null];
    for (let i = 1; i < n; i++) {
      const a = series[i - 1];
      const b = series[i];
      out.push(a != null && b != null ? (a + b) / 2 : null);
    }
    return out;
  };
  const annualized = (series) =>
    series.map((v, i) => {
      const q = quarterNo(dates[i]);
      return v == null || q === 0 ? null : (v / q) * 4;
    });
  const annCost = annualized(pointSeries("operating_cost"));
  const annRev = annualized(pointSeries("revenue"));
  const avgInv = avgPrevCur(inventoryPt);
  const avgRec = avgPrevCur(receivablesPt);
  const invDays = avgInv.map((avg, i) => {
    const ac = annCost[i];
    return avg == null || ac == null || ac <= 0 ? null : (365 * avg) / ac;
  });
  const recDays = avgRec.map((avg, i) => {
    const ar = annRev[i];
    return avg == null || ar == null || ar <= 0 ? null : (365 * avg) / ar;
  });

  const valueMap = {
    revenue: revQ,
    operating_cost: costQ,
    gross_profit: grossQ,
    net_profit: npQ,
    operating_cash_flow: ocfQ,
    interest_expense: ieQ,
    total_cash: cashPt,
    inventory: inventoryPt,
    accounts_receivable: receivablesPt,
    contract_liabilities: contractPt,
    short_term_borrowing: stBorrowPt,
    long_term_borrowing: ltBorrowPt,
    interest_bearing_debt: ibDebtPt,
    gross_margin: grossMargin,
    net_margin: netMargin,
    inventory_turnover_days: invDays,
    receivables_turnover_days: recDays,
  };

  const yoyIdx = records.map((_, i) => sameQuarterLastYear(records, i));

  const indicators = INDICATOR_DEFS.map(([key, name, unit, gtype]) => {
    const values = valueMap[key];
    const growth = [];
    const labels = [];
    for (let i = 0; i < n; i++) {
      const base = gtype === "yoy" ? yoyIdx[i] : i > 0 ? i - 1 : null;
      const bv = base != null ? values[base] : null;
      const [g, lab] = unit === "%" ? growthMargin(values[i], bv) : growthPct(values[i], bv);
      growth.push(g);
      labels.push(lab);
    }
    const fmt = (v) => (v == null ? null : unit === "亿" ? toYi(v) : round1(v));
    return {
      key,
      name,
      unit,
      growth_type: gtype,
      growth_name: gtype === "yoy" ? "同比增速" : "环比增速",
      values: values.map(fmt),
      growth,
      labels,
    };
  });

  // 计算基于全量，仅展示区间按 start/end 截取
  const idx = [];
  for (let i = 0; i < n; i++) {
    if ((!start || dates[i] >= start) && (!end || dates[i] <= end)) idx.push(i);
  }
  return {
    periods: idx.map((i) => dates[i]),
    indicators: indicators.map((ind) => ({
      ...ind,
      values: idx.map((i) => ind.values[i]),
      growth: idx.map((i) => ind.growth[i]),
      labels: idx.map((i) => ind.labels[i]),
    })),
  };
}

module.exports = { computeIndicators, INDICATOR_DEFS };
