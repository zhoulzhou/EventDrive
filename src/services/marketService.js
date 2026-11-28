"use strict";
/** 市场行情服务：抓取落库、展示聚合（涨跌幅 + 峰值回撤分级）、历史序列、清洗。*/
const marketRepo = require("../db/market");
const { getDb } = require("../db");
const { fetchAll } = require("../fetchers/marketQuotes");
const strategy = require("./marketStrategy");

const DISPLAY_ORDER = ["NASDAQ100", "VIXCLS", "DGS2", "DGS10", "GOLD", "BTCUSD"];

const nowStr = () => new Date().toISOString().replace("T", " ").slice(0, 19);

/** 抓取全市场并落库（按 symbol+date 去重；同日期同值跳过；推进峰值策略）。*/
async function fetchAndStore() {
  const usOpen = strategy.isUsMarketOpen();
  const items = await fetchAll(usOpen);
  const latest = marketRepo.getLatestTwoPerSymbol();
  let saved = 0, skipped = 0;
  for (const it of items) {
    if (it.value == null || !it.date) { skipped++; continue; }
    const cur = latest[it.symbol] && latest[it.symbol][0];
    if (cur && cur.date === it.date && cur.value === it.value) { skipped++; continue; }
    marketRepo.upsertPrice({ symbol: it.symbol, name: it.name, unit: it.unit, value: it.value, date: it.date });
    strategy.advance(it.symbol, it.value, it.date);
    saved++;
  }
  return { saved, skipped, total: items.length, us_open: usOpen, updated_at: nowStr() };
}

/** 展示数据：每个 symbol 最新值 + 涨跌幅 + 峰值回撤分级。空库时惰性抓取一次。*/
async function getDisplay() {
  let latest = marketRepo.getLatestTwoPerSymbol();
  if (Object.keys(latest).length === 0) {
    try { await fetchAndStore(); latest = marketRepo.getLatestTwoPerSymbol(); }
    catch (e) { console.error("[market] 惰性抓取失败:", e.message); }
  }
  const items = [];
  for (const [, rows] of Object.entries(latest)) {
    const cur = rows[0], prev = rows[1];
    let change = null, changePercent = null;
    if (cur.value != null && prev && prev.value) {
      change = cur.value - prev.value;
      changePercent = (change / prev.value) * 100;
    }
    items.push({
      name: cur.name, symbol: cur.symbol, unit: cur.unit || "",
      value: cur.value, change, change_percent: changePercent, date: cur.date, source: "FRED",
    });
  }
  const order = Object.fromEntries(DISPLAY_ORDER.map((s, i) => [s, i]));
  const filtered = items
    .filter((it) => order[it.symbol] != null)
    .sort((a, b) => order[a.symbol] - order[b.symbol]);

  for (const it of filtered) {
    if (it.value == null) { it.drawdown_level = "normal"; continue; }
    if (it.symbol === "VIXCLS") {
      const strat = strategy.getStrategy(it.symbol);
      Object.assign(it, strategy.computeLevel(it.value, strat ? strat.thresholds : [20, 30]));
    } else {
      const state = marketRepo.getStrategyState(it.symbol);
      Object.assign(it, strategy.computeDrawdown(it.symbol, it.value, state, it.date));
    }
  }
  return {
    status: "ok",
    items: filtered,
    updated_at: nowStr(),
    source: "纳斯达克 / CBOE / 美国财政部 / FRED / 新浪财经",
  };
}

async function getHistory(symbol) {
  const rows = marketRepo.getHistory(symbol);
  const points = rows.map((r) => ({ date: r.date, value: r.value }));
  let drawdownEvents = [];
  const strat = strategy.getStrategy(symbol);
  if (strat && strat.red_pct != null) {
    drawdownEvents = strategy.computeDrawdownEvents(points, strat.red_pct);
  }
  return {
    status: "ok",
    symbol,
    name: rows[0] ? rows[0].name : symbol,
    unit: rows[0] ? rows[0].unit || "" : "",
    points,
    drawdown_events: drawdownEvents,
  };
}

/** 清洗稀疏日期：4 个核心指标同日非空值 < 3 的日期删除。*/
function cleanupSparse() {
  const db = getDb();
  const syms = ["NASDAQ100", "VIXCLS", "DGS2", "DGS10"];
  const ph = syms.map(() => "?").join(",");
  const badDates = db
    .prepare(`SELECT date FROM market_prices WHERE symbol IN (${ph}) GROUP BY date HAVING COUNT(value) < 3`)
    .all(...syms)
    .map((r) => r.date)
    .filter(Boolean);
  if (!badDates.length) return 0;
  const dph = badDates.map(() => "?").join(",");
  const r = db
    .prepare(`DELETE FROM market_prices WHERE symbol IN (${ph}) AND date IN (${dph})`)
    .run(...syms, ...badDates);
  return r.changes;
}

module.exports = { fetchAndStore, getDisplay, getHistory, cleanupSparse, DISPLAY_ORDER };
