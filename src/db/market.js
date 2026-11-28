"use strict";
/** 行情 repo：价格按 (symbol,date) upsert、每 symbol 最新两条、历史序列、峰值、策略状态。*/
const { getDb } = require("./index");

function upsertPrice(rec) {
  const db = getDb();
  db.prepare(
    `INSERT INTO market_prices (symbol, name, unit, value, date) VALUES (?,?,?,?,?)
     ON CONFLICT(symbol, date) DO UPDATE SET name=excluded.name, unit=excluded.unit,
       value=excluded.value, updated_at=datetime('now')`
  ).run(rec.symbol, rec.name, rec.unit ?? null, rec.value ?? null, rec.date ?? null);
}

/** 每个 symbol 最新两条（用于显示最新值 + 计算涨跌幅）。返回 {symbol: [row,row]} */
function getLatestTwoPerSymbol() {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT m.id, m.symbol, m.name, m.unit, m.value, m.date
       FROM market_prices m
       JOIN (
         SELECT id, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC, id DESC) AS rn
         FROM market_prices
       ) x ON m.id = x.id
       WHERE x.rn <= 2
       ORDER BY m.symbol, m.date DESC`
    )
    .all();
  const out = {};
  for (const r of rows) (out[r.symbol] = out[r.symbol] || []).push(r);
  return out;
}

function getHistory(symbol) {
  const db = getDb();
  return db
    .prepare(
      `SELECT name, unit, value, date FROM market_prices WHERE symbol=? ORDER BY date ASC, id ASC`
    )
    .all(symbol);
}

/** 某 symbol 历史最大值记录（峰值）。*/
function getPeak(symbol) {
  const db = getDb();
  return db
    .prepare(
      `SELECT symbol, value, date FROM market_prices WHERE symbol=? ORDER BY value DESC, date DESC LIMIT 1`
    )
    .get(symbol);
}

function getStrategyState(symbol) {
  const db = getDb();
  return db
    .prepare(
      `SELECT symbol, peak_value, peak_date, drawdown_date FROM market_strategy_state WHERE symbol=?`
    )
    .get(symbol);
}

function upsertStrategyState({ symbol, peak_value, peak_date, drawdown_date }) {
  const db = getDb();
  db.prepare(
    `INSERT INTO market_strategy_state (symbol, peak_value, peak_date, drawdown_date) VALUES (?,?,?,?)
     ON CONFLICT(symbol) DO UPDATE SET peak_value=excluded.peak_value, peak_date=excluded.peak_date,
       drawdown_date=excluded.drawdown_date, updated_at=datetime('now')`
  ).run(symbol, peak_value, peak_date, drawdown_date ?? null);
}

module.exports = {
  upsertPrice,
  getLatestTwoPerSymbol,
  getHistory,
  getPeak,
  getStrategyState,
  upsertStrategyState,
};
