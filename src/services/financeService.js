"use strict";
/**
 * 财务三表 service：抓取入库 / 查询 / 股票列表。
 * 展示记录用显示名键（与抓取返回结构一致），指标计算基于 DB 列键行。
 */
const repo = require("../db/finance");
const { fetchFinancialReports } = require("../fetchers/financeSina");
const { computeIndicators } = require("./financeIndicators");

const DISPLAY_FIELDS = repo.FINANCIAL_FIELDS.map((f) => f.display);

function colRowsToDisplay(rows) {
  return rows.map((r) => {
    const rec = { 报告期: r.report_date };
    for (const f of repo.FINANCIAL_FIELDS) {
      rec[f.display] = r[f.col] == null ? null : Number(r[f.col]);
    }
    return rec;
  });
}

function latestName(rows) {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i] && rows[i].stock_name) return rows[i].stock_name;
  }
  return null;
}

function validatePeriod(start, end) {
  for (const [label, value] of [
    ["start", start],
    ["end", end],
  ]) {
    if (value == null || value === "") continue;
    const parts = String(value).split("-");
    if (parts.length !== 3 || !parts.every((p) => /^\d+$/.test(p))) {
      const err = new Error(`${label} 日期格式应为 YYYY-MM-DD，如 2024-03-31`);
      err.status = 400;
      throw err;
    }
  }
}

/** 抓取并入库。start/end 仅截取本次入库范围。 */
async function fetch(code, name, start, end) {
  validatePeriod(start, end);
  const codeStr = String(code || "").trim();
  if (!codeStr) {
    const e = new Error("请提供股票代码");
    e.status = 400;
    throw e;
  }
  const { records, stockName } = await fetchFinancialReports(codeStr);
  let recs = records;
  if (start || end) {
    recs = recs.filter(
      (r) => (!start || r["报告期"] >= start) && (!end || r["报告期"] <= end)
    );
  }
  const finalName = (name && String(name).trim()) || stockName || null;
  const saved = repo.upsertReports(codeStr, recs, finalName);
  return { saved, records: recs, fields: DISPLAY_FIELDS, stockName: finalName };
}

/** 查询已入库数据（不重新抓取）。指标计算基于全量，展示按 start/end 截取。*/
function query(code, name, start, end) {
  validatePeriod(start, end);
  const c = code ? String(code).trim() : null;
  const nm = name ? String(name).trim() : null;
  if (!c && !nm) {
    const e = new Error("请提供股票代码或股票名称");
    e.status = 400;
    throw e;
  }
  let rows = [];
  if (c) {
    rows = repo.listReports(c);
  } else {
    const hit = repo.listCodes().find((s) => s.name === nm);
    if (hit) rows = repo.listReports(hit.code);
  }
  const indicators = computeIndicators(rows, start, end);
  const filtered = rows.filter(
    (r) => (!start || r.report_date >= start) && (!end || r.report_date <= end)
  );
  return {
    code: c,
    name: nm || latestName(rows),
    fields: DISPLAY_FIELDS,
    records: colRowsToDisplay(filtered),
    periods: indicators.periods,
    indicators: indicators.indicators,
  };
}

function listStocks() {
  return repo.listCodes();
}

module.exports = { fetch, query, listStocks, DISPLAY_FIELDS };
