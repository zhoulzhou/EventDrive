"use strict";
/**
 * 全球行情多源抓取（移植自 app/crawlers/market_data.py）。
 * 源：纳斯达克官方(主)+FRED(备) / CBOE / 美国财政部 / 新浪财经。
 * 每个源独立 try/catch，失败返回 {value:null}，由上层决定是否落库（不落空值）。
 */
const client = require("./httpClient");
const { SOURCES, UA } = require("./sources");
const { asJson, parseCsv, parseSina, toFloat } = require("./parsers");

const NASDAQ_HEADERS = {
  "User-Agent": UA,
  Accept: "application/json, text/plain, */*",
  "Accept-Language": "en-US,en;q=0.9",
};

const SERIES = [
  { key: "nasdaq", name: "纳斯达克100指数", symbol: "NASDAQ100", unit: "", source: "nasdaq", column: null, describe: "NASDAQ-100 Index", backup: "fred" },
  { key: "vix", name: "VIX恐慌指数", symbol: "VIXCLS", unit: "", source: "cboe", column: null, describe: "CBOE Volatility Index" },
  { key: "dgs2", name: "美债2年期收益率", symbol: "DGS2", unit: "%", source: "treasury", column: "2 Yr", describe: "U.S. Treasury 2-Year Yield" },
  { key: "dgs10", name: "美债10年期收益率", symbol: "DGS10", unit: "%", source: "treasury", column: "10 Yr", describe: "U.S. Treasury 10-Year Yield" },
  { key: "gold", name: "黄金价格（伦敦金）", symbol: "GOLD", unit: "美元/盎司", source: "sina", column: null, code: "hf_XAU", describe: "Spot Gold XAU/USD" },
  { key: "btc", name: "比特币", symbol: "BTCUSD", unit: "美元", source: "sina", column: null, code: "btc_btcbtcusd", describe: "Bitcoin USD" },
];

function ymdMDY(s) { const [m, d, y] = s.split("/"); return `${y}-${m}-${d}`; }

const MONTHS = { Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12 };
function parseNasdaqTs(ts) {
  try {
    const parts = ts.replace(/,/g, "").split(/\s+/);
    if (parts.length !== 3) return "";
    const [mon, day, year] = parts;
    return `${year}-${String(MONTHS[mon.slice(0, 3)]).padStart(2, "0")}-${String(parseInt(day, 10)).padStart(2, "0")}`;
  } catch (_) { return ""; }
}

async function fetchFred(symbol) {
  const url = `https://fred.stlouisfed.org/graph/fredgraph.csv?id=${symbol}`;
  const r = await client.get(url, { "User-Agent": UA }, { noCache: true });
  const rows = parseCsv(r.body);
  const vals = [];
  for (const row of rows.slice(1)) {
    if (row.length < 2) continue;
    const date = (row[0] || "").trim();
    const raw = (row[1] || "").trim();
    if (!raw || raw === "." || raw === "") continue;
    const v = toFloat(raw);
    if (v != null) vals.push([date, v]);
  }
  if (!vals.length) return { value: null, date: null };
  const [date, value] = vals[vals.length - 1];
  return { value, date };
}

async function fetchVix() {
  const url = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv";
  const r = await client.get(url, SOURCES.cboe, { noCache: true });
  const rows = parseCsv(r.body);
  let latest = null;
  for (const row of rows.slice(1)) {
    if (row.length < 5 || !row[0]) continue;
    const close = (row[4] || "").trim();
    if (!close) continue;
    const v = toFloat(close);
    if (v != null) latest = [row[0].trim(), v];
  }
  if (!latest) return { value: null, date: null };
  return { value: latest[1], date: ymdMDY(latest[0]) };
}

async function fetchNasdaq() {
  const url = "https://api.nasdaq.com/api/quote/NDX/info?assetclass=index";
  const r = await client.get(url, NASDAQ_HEADERS, { noCache: true });
  const j = asJson(r.body);
  const pd = j && j.data && j.data.primaryData;
  if (!pd) return { value: null, date: null };
  const lastSale = String(pd.lastSalePrice || "").replace(/,/g, "").trim();
  const ts = String(pd.lastTradeTimestamp || "").trim();
  if (!lastSale || !ts) return { value: null, date: null };
  const value = toFloat(lastSale);
  const date = parseNasdaqTs(ts);
  if (value == null || !date) return { value: null, date: null };
  return { value, date };
}

async function fetchTreasury(column) {
  const year = new Date().getFullYear();
  const url = `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/${year}/all?type=daily_treasury_yield_curve&field_tdr_date_value=${year}&page&_format=csv`;
  const r = await client.get(url, SOURCES.treasury, { noCache: true });
  const rows = parseCsv(r.body);
  if (!rows.length) return { value: null, date: null };
  const header = rows[0].map((c) => (c || "").trim());
  const idx = header.indexOf(column);
  if (idx < 0) return { value: null, date: null };
  for (const row of rows.slice(1)) {
    if (row.length <= idx) continue;
    const dateRaw = (row[0] || "").trim();
    const valRaw = (row[idx] || "").trim();
    if (!dateRaw || !valRaw) continue;
    const v = toFloat(valRaw);
    if (v != null) return { value: v, date: ymdMDY(dateRaw) };
  }
  return { value: null, date: null };
}

async function fetchSina(code) {
  const url = `https://hq.sinajs.cn/list=${code}`;
  const r = await client.get(url, SOURCES.sina, { noCache: true });
  const parts = parseSina(r.body);
  if (!parts) return { value: null, date: null };
  const priceAt = code.startsWith("btc_") ? 3 : 0;
  const value = toFloat(parts[priceAt]);
  if (value == null || value <= 0) return { value: null, date: null };
  const quoteDate = parts.find((p) => /^\d{4}-\d{2}-\d{2}$/.test((p || "").trim()));
  const date = quoteDate ? quoteDate.trim() : new Date().toISOString().slice(0, 10);
  return { value, date };
}

async function fetchSeries(series) {
  let result;
  try {
    if (series.source === "nasdaq") result = await fetchNasdaq();
    else if (series.source === "fred") result = await fetchFred(series.symbol);
    else if (series.source === "cboe") result = await fetchVix();
    else if (series.source === "treasury") result = await fetchTreasury(series.column);
    else if (series.source === "sina") result = await fetchSina(series.code);
    else return { value: null, date: null };
  } catch (e) {
    result = { value: null, date: null };
  }
  // 主源无有效值时回退备用源
  if ((result.value == null) && series.backup === "fred") {
    try { result = await fetchFred(series.symbol); } catch (_) {}
  }
  return result;
}

/** 并发抓取全部指标（美股休市仅抓黄金/比特币）。*/
async function fetchAll(usOpen) {
  const targets = usOpen ? SERIES : SERIES.filter((s) => s.source === "sina");
  const raws = await Promise.all(targets.map((s) => fetchSeries(s)));
  return targets.map((s, i) => ({
    key: s.key, name: s.name, symbol: s.symbol, unit: s.unit,
    describe: s.describe, value: raws[i].value, date: raws[i].date, source: s.source,
  }));
}

module.exports = { fetchAll, SERIES };
