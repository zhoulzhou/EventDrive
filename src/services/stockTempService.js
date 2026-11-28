"use strict";
/** 股市温度服务：多源抓取落库、展示聚合、历史序列、贪婪指数打卡。
 *
 * 忠实对齐 app/utils/stock_temp_refresh.py + app/api/stock_temp.py：
 *  - 抓取失败的指标记 error，保留上一次成功读数，绝不写兜底值（AC-12）。
 *  - 不做历史回填：只落当日读数 + 抓取自然带的近期序列，历史逐日积累。
 *  - 分位按 ≥12 期才算；样本不足时卡片显式提示，温度层按可用项重标权重。
 */
const config = require("../config");
const { stockTempRepo } = require("../db/indicators");
const st = require("./stockTempIndicators");
const em = require("../fetchers/stockTempEastmoney");
const legu = require("../fetchers/stockTempLegu");

const nowStr = () => new Date().toISOString().replace("T", " ").slice(0, 19);
function cnStr(ts) {
  if (!ts) return "—";
  const d = new Date(ts.endsWith("Z") ? ts : ts.replace(" ", "T") + "Z");
  if (isNaN(d.getTime())) return String(ts);
  const bj = new Date(d.getTime() + 8 * 3600 * 1000);
  return bj.toISOString().replace("T", " ").slice(0, 19);
}

let fetching = false;
let lastErrors = {};

function freqCode(ind) {
  const f = String(ind.freq || "");
  if (f.startsWith("日")) return "D";
  if (f.startsWith("周")) return "W";
  return "M";
}

/** 抓取全部指标（失败项记 error，不抛出）。返回 {key: item}。*/
async function fetchAll(prev) {
  prev = prev || {};
  const fetched = {};
  const setErr = (key, e) => {
    fetched[key] = { error: (e && e.message) || String(e) };
  };

  // 先抓市场汇总（含流通市值，两融占比依赖它）
  let floatMcapYi = null;
  try {
    const m = await em.fetchMarketStats(require("../fetchers/httpClient"));
    Object.assign(fetched, {
      turnover_amt: m.turnover_amt,
      turnover_rate: m.turnover_rate,
      total_float_mcap: m.total_float_mcap,
    });
    floatMcapYi = m._float_mcap_yi;
  } catch (e) {
    setErr("turnover_amt", e);
    setErr("turnover_rate", e);
    setErr("total_float_mcap", e);
  }

  // 并行抓其余（httpClient 内部有序列化门控）
  const tasks = {
    margin: () => em.fetchMargin(require("../fetchers/httpClient"), floatMcapYi),
    etf_share: () => em.fetchEtfShare(require("../fetchers/httpClient"), prev.etf_share),
    new_fund: () => em.fetchNewFund(require("../fetchers/httpClient")),
    industry_capital: () => em.fetchIndustryCapital(require("../fetchers/httpClient")),
    structure_div: () => em.fetchStructureDiv(require("../fetchers/httpClient")),
    pe_pb: () => legu.fetchPePb(require("../fetchers/httpClient")),
    below_na: () => legu.fetchBelowNa(require("../fetchers/httpClient")),
    breadth: () => legu.fetchBreadth(require("../fetchers/httpClient")),
  };
  for (const [k, fn] of Object.entries(tasks)) {
    try {
      const res = await fn();
      if (k === "pe_pb") {
        Object.assign(fetched, res);
      } else {
        fetched[k] = res;
      }
    } catch (e) {
      setErr(k, e);
    }
  }

  // 无可用数据源的指标：直接标记 error（不写库、不兜底）
  for (const k of ["erp", "fund_position"]) {
    if (!fetched[k]) fetched[k] = { error: "暂无可用数据源" };
  }
  return fetched;
}

/** 给分位判定但本次未算分位的指标，用历史表补算并回写快照。*/
function fillPercentiles(records) {
  const need = records.filter((rec) => {
    const ind = st.INDICATOR_MAP[rec.key];
    if (!ind || ind.judge !== "percentile") return false;
    return st.loadDetail(rec.detail)["分位"] == null;
  });
  if (!need.length) return 0;
  const keys = need.map((r) => r.key);
  const grouped = stockTempRepo.getHistorySeries(keys, 6000);
  const fixed = [];
  for (const rec of need) {
    const ind = st.INDICATOR_MAP[rec.key];
    const rows = grouped[rec.key] || [];
    const series = rows.filter((r) => r.value != null && r.as_of).map((r) => ({ as_of: r.as_of, value: r.value }));
    const [pct, window] = st.percentileRank(rec.value, series, freqCode(ind));
    if (pct == null) continue;
    const detail = st.loadDetail(rec.detail);
    detail["分位"] = pct;
    detail["分位窗口"] = window;
    detail["分位来源"] = "由本页历史序列补算";
    rec.detail = detail;
    fixed.push(rec);
  }
  if (fixed.length) stockTempRepo.upsertLatest(fixed);
  return fixed.length;
}

/** 把当前贪婪指数打卡写进历史表（key="greed"）。*/
function recordGreed() {
  const raw = buildRaw();
  const gkeys = [...st.GREED_KEYS, ...st.GREED_CONFIRM_KEYS];
  const grouped = stockTempRepo.getHistorySeries(gkeys, 8000);
  const seriesMap = {};
  for (const k of gkeys) {
    seriesMap[k] = (grouped[k] || [])
      .filter((r) => r.value != null && r.as_of)
      .map((r) => ({ as_of: r.as_of, value: r.value }));
  }
  const payload = st.buildResponse(raw, seriesMap);
  const greed = payload.greed || {};
  if (greed.value == null) return 0;
  const detail = {};
  for (const comp of greed.components || []) {
    if (comp.score != null) detail[comp.name + "分位"] = comp.score;
  }
  detail["情绪分"] = (greed.sentiment || {}).score;
  detail["估值分"] = (greed.valuation || {}).score;
  detail["深度恐慌"] = (greed.gates || {}).deep_fear && greed.gates.deep_fear.triggered ? 1 : 0;
  detail["泡沫警示"] = (greed.gates || {}).bubble && greed.gates.bubble.triggered ? 1 : 0;
  detail["数据源"] = "全历史分位加权";
  const asOf =
    (greed.components || []).map((c) => c.as_of).filter(Boolean).sort().slice(-1)[0] ||
    em.todayCST();
  const r = stockTempRepo.bulkUpsertHistory([
    { key: "greed", value: greed.value, as_of: asOf, source: "eastmoney", detail },
  ]);
  return (r && r.added) || 0;
}

async function fetchAndStore() {
  if (fetching) return { status: "busy" };
  fetching = true;
  try {
    const prevRows = stockTempRepo.getAllLatest();
    const prev = {};
    for (const r of prevRows) prev[r.key] = { value: r.value, as_of: r.as_of, source: r.source, detail: r.detail };

    const fetched = await fetchAll(prev);
    lastErrors = {};
    const records = [];
    const historyExtras = [];
    for (const [key, item] of Object.entries(fetched)) {
      if (!item || item.error) {
        if (item && item.error) lastErrors[key] = item.error;
        continue;
      }
      if (item.value == null || !item.as_of) continue;
      const rec = {
        key,
        value: item.value,
        as_of: item.as_of,
        source: item.source || "eastmoney",
        detail: item.detail || {},
      };
      records.push(rec);
      if (Array.isArray(item.series)) {
        for (const p of item.series) {
          if (p.value != null && p.as_of) {
            historyExtras.push({ key, value: p.value, as_of: p.as_of, source: item.source || "eastmoney", detail: {} });
          }
        }
      }
    }

    let refreshed = 0;
    let added = 0;
    if (records.length) {
      refreshed = stockTempRepo.upsertLatest(records);
      const w = stockTempRepo.bulkUpsertHistory([...records, ...historyExtras]);
      added = (w && w.added) || 0;
      if (fillPercentiles(records)) {
        // 分位补算后快照已回写；历史值未变，无需重复写历史
      }
    }
    added += recordGreed();
    return { refreshed, errors: lastErrors, history_added: added, updated_at: nowStr() };
  } finally {
    fetching = false;
  }
}

function buildRaw() {
  const rows = stockTempRepo.getAllLatest();
  const raw = {};
  for (const r of rows) {
    raw[r.key] = {
      value: r.value,
      as_of: r.as_of,
      source: r.source,
      detail: r.detail || {},
      error: null,
    };
  }
  return raw;
}

/** 读取时用历史补算分位（仅内存，不落库；刷新时已落库）。*/
function computePercentilesOnRead(raw) {
  const need = st.INDICATOR_KEYS.filter((k) => {
    const ind = st.INDICATOR_MAP[k];
    return ind.judge === "percentile" && raw[k] && raw[k].value != null && st.loadDetail(raw[k].detail)["分位"] == null;
  });
  if (!need.length) return;
  const grouped = stockTempRepo.getHistorySeries(need, 6000);
  for (const k of need) {
    const ind = st.INDICATOR_MAP[k];
    const rows = (grouped[k] || []).filter((r) => r.value != null && r.as_of).map((r) => ({ as_of: r.as_of, value: r.value }));
    const [pct, window] = st.percentileRank(raw[k].value, rows, freqCode(ind));
    if (pct != null) {
      raw[k].detail = { ...raw[k].detail, 分位: pct, 分位窗口: window, 分位来源: "由本页历史序列补算" };
    }
  }
}

function buildSeriesMapForGreed() {
  const gkeys = [...st.GREED_KEYS, ...st.GREED_CONFIRM_KEYS];
  const grouped = stockTempRepo.getHistorySeries(gkeys, 8000);
  const map = {};
  for (const k of gkeys) {
    map[k] = (grouped[k] || [])
      .filter((r) => r.value != null && r.as_of)
      .map((r) => ({ as_of: r.as_of, value: r.value }));
  }
  return map;
}

function freshness() {
  const rows = stockTempRepo.getAllLatest().filter((r) => st.AUTO_KEYS.includes(r.key) && r.updated_at);
  if (!rows.length) return { updated_at: "—", data_age_hours: null, stale: false };
  const latest = rows.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
  const ts = Date.parse(latest.updated_at.replace(" ", "T") + "Z");
  const age = ts ? Math.round(((Date.now() - ts) / 3600000) * 10) / 10 : null;
  return { updated_at: cnStr(latest.updated_at), data_age_hours: age, stale: age != null && age >= 30 };
}

function buildPayload(refreshed = 0, errors = {}, historyAdded = 0) {
  const raw = buildRaw();
  for (const [k, msg] of Object.entries(errors)) if (raw[k]) raw[k].error = msg;
  computePercentilesOnRead(raw);
  const payload = st.buildResponse(raw, buildSeriesMapForGreed());
  payload.refreshed = refreshed;
  payload.errors = errors;
  payload.history_added = historyAdded;
  const f = freshness();
  payload.updated_at = f.updated_at;
  payload.data_age_hours = f.data_age_hours;
  payload.stale = f.stale;
  payload.history_total = stockTempRepo.countHistory();
  payload.history_note =
    "所有数据自部署起逐日积累，不做历史回填。分位样本不足 12 期时按读数区间判读并显式提示；积累充足后以分位为准。";
  return payload;
}

function getDisplay() {
  const hasData = stockTempRepo.getAllLatest().some((r) => st.AUTO_KEYS.includes(r.key));
  const age = freshness().data_age_hours;
  const stale = age != null && age > config.stockTempCacheTtlHours;
  if ((!hasData || stale) && !fetching) {
    fetchAndStore().catch((e) => console.error("[stock-temp] 惰性抓取失败:", e && e.message));
  }
  return buildPayload(0, lastErrors, 0);
}

async function refresh() {
  const res = await fetchAndStore();
  return buildPayload(res.refreshed || 0, res.errors || {}, res.history_added || 0);
}

function getSeries() {
  const grouped = stockTempRepo.getHistorySeries(st.CHART_KEYS.filter((k) => k !== "temperature" && k !== "greed"), 6000);
  const bag = {};
  for (const k of st.CHART_KEYS) {
    if (k === "temperature") {
      const seriesMap = {};
      for (const hk of st.CHART_KEYS) {
        if (hk === "temperature" || hk === "greed") continue;
        seriesMap[hk] = (grouped[hk] || [])
          .filter((r) => r.value != null && r.as_of)
          .map((r) => ({ as_of: r.as_of, value: r.value }));
      }
      bag.temperature = st.reconstructTemperature(seriesMap).monthly.map((m) => ({ as_of: m.as_of, value: m.total }));
      continue;
    }
    if (k === "greed") {
      const greedRows = stockTempRepo.getHistory("greed", 6000, "asc") || [];
      bag.greed = greedRows
        .filter((r) => r.value != null && r.as_of)
        .map((r) => ({ as_of: r.as_of, value: r.value }));
      continue;
    }
    bag[k] = (grouped[k] || [])
      .filter((r) => r.value != null && r.as_of)
      .map((r) => ({ as_of: r.as_of, value: r.value }));
  }
  const f = freshness();
  return {
    status: "ok",
    as_of: f.updated_at,
    stale: f.stale,
    series: bag,
  };
}

function getHistory(key, limit = 200, order = "desc") {
  const rows = stockTempRepo.getHistory(key, limit, order);
  const records = rows.map((r) => ({
    key: r.key,
    name: (st.INDICATOR_MAP[r.key] && st.INDICATOR_MAP[r.key].name) || r.key,
    value: r.value,
    as_of: r.as_of,
    source: r.source,
    source_label: st.SOURCE_LABELS[r.source] || r.source,
    fetched_at: r.fetched_at || null,
    detail: r.detail || {},
  }));
  return {
    status: "ok",
    key,
    order,
    count: records.length,
    total: stockTempRepo.countHistory(key),
    records,
  };
}

module.exports = { getDisplay, refresh, getSeries, getHistory, fetchAndStore };
