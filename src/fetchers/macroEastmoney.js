"use strict";
/**
 * 宏观指标 · 东方财富数据中心源。
 * 取最新一期：M1/M2 同比（货币供应量）、制造业 PMI、PPI 同比。
 * 源稳定可靠（JSON 接口），失败抛错由上层按「该指标缺失」处理。
 */
const client = require("./httpClient");
const { SOURCES } = require("./sources");
const { asJson, toFloat } = require("./parsers");

const BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get";
const SOURCE = "eastmoney";

function ymOf(row) {
  // REPORT_DATE "2026-10-01 00:00:00" → "2026-10"
  const s = String((row && row.REPORT_DATE) || "").slice(0, 7);
  if (/^\d{4}-\d{2}$/.test(s)) return s;
  const m = String((row && row.TIME) || "").match(/(\d{4})\D+(\d{1,2})/);
  return m ? `${m[1]}-${String(m[2]).padStart(2, "0")}` : "";
}

async function latestRow(reportName) {
  const url =
    `${BASE}?reportName=${reportName}&columns=ALL&source=WEB&client=WEB` +
    `&pageNumber=1&pageSize=10&sortColumns=REPORT_DATE&sortTypes=-1`;
  const r = await client.get(url, SOURCES.em_data, { noCache: true });
  if (r.status < 200 || r.status >= 300) throw new Error(`HTTP ${r.status}`);
  const j = asJson(r.body);
  const data = (j && j.result && j.result.data) || [];
  if (!data.length) throw new Error(`${reportName} 返回空数据`);
  return data[0];
}

async function fetchMoneySupply() {
  const row = await latestRow("RPT_ECONOMY_CURRENCY_SUPPLY");
  const m2 = toFloat(row.BASIC_CURRENCY_SAME); // M2 同比
  const m1 = toFloat(row.CURRENCY_SAME); // M1 同比
  if (m2 == null || m1 == null) throw new Error("货币供应量缺少 M1/M2 同比字段");
  const asOf = ymOf(row);
  const detail = { M1同比: m1, M2同比: m2 };
  return {
    records: [
      { key: "m1_m2", value: Math.round((m1 - m2) * 100) / 100, as_of: asOf, source: SOURCE, detail },
    ],
    // 趋势图专用序列（只入历史表，不作为卡片）
    historyExtras: [
      { key: "m1_yoy", value: m1, as_of: asOf, source: SOURCE, detail: {} },
      { key: "m2_yoy", value: m2, as_of: asOf, source: SOURCE, detail: {} },
    ],
  };
}

async function fetchPmi() {
  const row = await latestRow("RPT_ECONOMY_PMI");
  const v = toFloat(row.MAKE_INDEX);
  if (v == null) throw new Error("PMI 缺少 MAKE_INDEX 字段");
  return {
    records: [
      {
        key: "pmi", value: v, as_of: ymOf(row), source: SOURCE,
        detail: { 非制造业: toFloat(row.NMAKE_INDEX) },
      },
    ],
    historyExtras: [],
  };
}

async function fetchPpi() {
  const row = await latestRow("RPT_ECONOMY_PPI");
  const v = toFloat(row.BASE_SAME); // PPI 当月同比
  if (v == null) throw new Error("PPI 缺少 BASE_SAME 字段");
  return { records: [{ key: "ppi_yoy", value: v, as_of: ymOf(row), source: SOURCE, detail: {} }], historyExtras: [] };
}

/** 并发抓取全部东方财富宏观指标。返回 { records, historyExtras }。*/
async function fetchAll() {
  const results = await Promise.allSettled([fetchMoneySupply(), fetchPmi(), fetchPpi()]);
  const records = [];
  const historyExtras = [];
  for (const r of results) {
    if (r.status === "fulfilled") {
      records.push(...r.value.records);
      historyExtras.push(...r.value.historyExtras);
    }
    // rejected 的指标不入库（不写空值），由上层记录 error
  }
  return { records, historyExtras };
}

module.exports = { fetchAll, fetchMoneySupply, fetchPmi, fetchPpi };
