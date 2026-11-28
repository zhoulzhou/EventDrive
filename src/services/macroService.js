"use strict";
/** 宏观指标服务：多源抓取落库、展示聚合、历史序列。无兜底、无回溯。*/
const config = require("../config");
const { macroRepo } = require("../db/indicators");
const mi = require("./macroIndicators");
const em = require("../fetchers/macroEastmoney");
const pbc = require("../fetchers/macroPbc");

const nowStr = () => new Date().toISOString().replace("T", " ").slice(0, 19);
// 北京时间字符串（库存 UTC）
function cnStr(ts) {
  if (!ts) return "—";
  const d = new Date(ts.endsWith("Z") ? ts : ts.replace(" ", "T") + "Z");
  if (isNaN(d.getTime())) return String(ts);
  const bj = new Date(d.getTime() + 8 * 3600 * 1000);
  return bj.toISOString().replace("T", " ").slice(0, 19);
}

let fetching = false;

/** 抓取全部宏观指标并落库（只写成功项；读数变化才追加历史）。*/
async function fetchAndStore() {
  if (fetching) return { status: "busy" };
  fetching = true;
  try {
    const [emRes, pbcRes] = await Promise.all([em.fetchAll(), pbc.fetchAll()]);
    const errors = { ...(pbcRes.errors || {}) };

    // 合并记录，按 key 去重（东方财富优先，央行补其余）
    const byKey = new Map();
    for (const rec of [...emRes.records, ...pbcRes.records]) {
      if (!rec || rec.value == null || !rec.as_of) continue;
      if (!byKey.has(rec.key)) byKey.set(rec.key, rec);
    }
    const records = [...byKey.values()];

    const existing = macroRepo.getAllLatest();
    const existingMap = new Map(existing.map((r) => [r.key, r]));

    const toWrite = [];
    const historyRecs = [];
    for (const rec of records) {
      const cur = existingMap.get(rec.key);
      if (cur && cur.as_of === rec.as_of && Number(cur.value) === Number(rec.value)) continue; // 未变跳过
      toWrite.push(rec);
      historyRecs.push(rec);
    }
    // 额外历史序列（M1/M2 同比）
    for (const ex of emRes.historyExtras || []) {
      if (ex.value != null && ex.as_of) historyRecs.push(ex);
    }

    let refreshed = 0, historyAdded = 0;
    if (toWrite.length) {
      refreshed = macroRepo.upsertLatest(toWrite);
    }
    if (historyRecs.length) {
      const r = macroRepo.bulkUpsertHistory(historyRecs);
      historyAdded = (r && r.added) || 0;
    }
    return { refreshed, errors, history_added: historyAdded, updated_at: nowStr() };
  } finally {
    fetching = false;
  }
}

function buildRaw() {
  const rows = macroRepo.getAllLatest();
  const raw = {};
  for (const r of rows) {
    raw[r.key] = {
      value: r.value, as_of: r.as_of, source: r.source,
      source_detail: mi.sourceNote(r.detail), detail: r.detail || {}, error: null,
    };
  }
  return raw;
}

function loadParams() {
  const row = macroRepo.getLatest(mi.PARAM_REVERSE_REPO);
  if (!row || row.value == null) return {};
  const meta = mi.PARAM_META[mi.PARAM_REVERSE_REPO];
  const term = (row.detail && row.detail["期限"]) || "";
  const name = term ? `${term}逆回购操作利率（政策利率锚）` : meta.name;
  return {
    [mi.PARAM_REVERSE_REPO]: {
      key: mi.PARAM_REVERSE_REPO, name, unit: meta.unit, digits: meta.digits, hint: meta.hint,
      value: row.value, as_of: row.as_of || "—", source: row.source || "pbc",
      source_label: mi.SOURCE_LABELS[row.source] || row.source,
      source_detail: mi.sourceNote(row.detail),
    },
  };
}

function freshness() {
  const rows = macroRepo.getAllLatest().filter((r) => mi.AUTO_KEYS.includes(r.key) && r.updated_at);
  if (!rows.length) return { updated_at: "—", data_age_hours: null, stale: false };
  const latest = rows.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
  const ts = Date.parse(latest.updated_at.replace(" ", "T") + "Z");
  const age = ts ? Math.round(((Date.now() - ts) / 3600000) * 10) / 10 : null;
  return { updated_at: cnStr(latest.updated_at), data_age_hours: age, stale: age != null && age >= 26 };
}

function buildPayload(refreshed = 0, errors = {}, historyAdded = 0) {
  const raw = buildRaw();
  for (const [k, msg] of Object.entries(errors)) if (raw[k]) raw[k].error = msg;
  const payload = mi.buildResponse(raw, loadParams());
  payload.refreshed = refreshed;
  payload.errors = errors;
  payload.history_added = historyAdded;
  const f = freshness();
  payload.updated_at = f.updated_at;
  payload.data_age_hours = f.data_age_hours;
  payload.stale = f.stale;
  payload.history_total = macroRepo.countHistory();
  return payload;
}

/** 展示数据：只读库；空库或数据过期时后台触发一次惰性抓取（不阻塞响应）。*/
function getDisplay() {
  const hasData = macroRepo.getAllLatest().some((r) => mi.AUTO_KEYS.includes(r.key));
  const age = freshness().data_age_hours;
  const stale = age != null && age > config.macroCacheTtlHours;
  if ((!hasData || stale) && !fetching) {
    fetchAndStore().catch((e) => console.error("[macro] 惰性抓取失败:", e && e.message));
  }
  return buildPayload();
}

async function refresh() {
  const res = await fetchAndStore();
  return buildPayload(res.refreshed || 0, res.errors || {}, res.history_added || 0);
}

function getHistory(key, limit = 200, order = "desc") {
  const rows = macroRepo.getHistory(key, limit, order);
  const records = rows.map((r) => ({
    key: r.key, name: mi.NAME_OF_KEY[r.key] || r.key,
    value: r.value, as_of: r.as_of, source: r.source,
    source_label: mi.SOURCE_LABELS[r.source] || r.source,
    fetched_at: r.fetched_at || null, detail: r.detail || {},
  }));
  return { status: "ok", key, order, count: records.length, total: macroRepo.countHistory(key), records };
}

module.exports = { getDisplay, refresh, getHistory, fetchAndStore };
