"use strict";
/**
 * 两阶段 DCF 估值（净利润替代 FCFF）：高增长期 → 永续期。
 * 移植自 app/api/valuation.py。百分比以百分数值传入（如 10 表示 10%）。
 */
const repo = require("../db/valuation");

const r2 = (v) => (v == null ? null : Math.round(Number(v) * 100) / 100);

function validate(input) {
  const errs = [];
  if (!input || typeof input !== "object") errs.push("参数无效");
  const name = input && input.company_name != null ? String(input.company_name).trim() : "";
  if (!name) errs.push("企业名称不能为空");
  const base = Number(input && input.base_profit);
  const fy = Number(input && input.forecast_years);
  const gf = Number(input && input.growth_forecast);
  const gp = Number(input && input.growth_perpetual);
  const dr = Number(input && input.discount_rate);
  if (!Number.isFinite(base)) errs.push("基期净利润需为数字");
  if (!Number.isFinite(fy) || fy < 1) errs.push("高增长期年限需大于 0");
  if (!Number.isFinite(gf)) errs.push("高增长期增长率需为数字");
  if (!Number.isFinite(gp)) errs.push("永续增长率需为数字");
  if (!Number.isFinite(dr)) errs.push("折现率需为数字");
  if (errs.length) {
    const e = new Error(errs.join("；"));
    e.status = 400;
    throw e;
  }
  return { name, base, fy: Math.floor(fy), gf, gp, dr };
}

/** 纯计算，不入库。返回企业整体价值（亿元）。*/
function computeValuation(input) {
  const { base, fy, gf, gp, dr } = validate(input);
  const gHigh = gf / 100;
  const gPerp = gp / 100;
  let discount = dr / 100;
  // 折现率需大于永续增长率，否则自动上调（与页面提示一致）
  if (discount <= gPerp) discount = gPerp + 0.03;

  let totalPV = 0;
  for (let y = 1; y <= fy; y++) {
    totalPV += (base * Math.pow(1 + gHigh, y)) / Math.pow(1 + discount, y);
  }
  const profitEnd = base * Math.pow(1 + gHigh, fy);
  const terminalValue = (profitEnd * (1 + gPerp)) / (discount - gPerp);
  const terminalPV = terminalValue / Math.pow(1 + discount, fy);
  return totalPV + terminalPV;
}

/** 计算并入库，返回 { id, enterprise_value, history }。*/
function calculateAndStore(input) {
  const { name, base, fy, gf, gp, dr } = validate(input);
  const value = computeValuation(input);
  const cmv =
    input.current_market_value === null ||
    input.current_market_value === undefined ||
    input.current_market_value === ""
      ? null
      : Number(input.current_market_value);
  const id = repo.insertValuation({
    company_name: name,
    base_profit: r2(base),
    forecast_years: fy,
    growth_forecast: r2(gf),
    growth_perpetual: r2(gp),
    discount_rate: r2(dr),
    current_market_value: cmv == null || !Number.isFinite(cmv) ? null : r2(cmv),
    enterprise_value: r2(value),
  });
  return { id, enterprise_value: r2(value), history: repo.listValuations(100) };
}

function listHistory(limit) {
  const n = Math.min(Math.max(parseInt(limit, 10) || 100, 1), 100);
  return repo.listValuations(n);
}

function deleteById(id) {
  const n = Number(id);
  if (!Number.isInteger(n)) {
    const e = new Error("记录 id 无效");
    e.status = 400;
    throw e;
  }
  const ok = repo.deleteById(n);
  if (!ok) {
    const e = new Error("记录不存在");
    e.status = 404;
    throw e;
  }
  return n;
}

function clearAll() {
  return repo.deleteAll();
}

module.exports = { computeValuation, calculateAndStore, listHistory, deleteById, clearAll };
