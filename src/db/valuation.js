"use strict";
/** 估值 repo：插入（裁剪至最近 100 条）、列表、按 id 删除、清空。*/
const { getDb } = require("./index");

const COLS =
  "company_name, base_profit, forecast_years, growth_forecast, transition_years, growth_transition, growth_perpetual, discount_rate, current_market_value, enterprise_value";

function insertValuation(v) {
  const db = getDb();
  return db.transaction(() => {
    const r = db
      .prepare(
        `INSERT INTO company_valuations (${COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)`
      )
      .run(
        v.company_name,
        v.base_profit,
        v.forecast_years,
        v.growth_forecast,
        v.transition_years ?? null,
        v.growth_transition ?? null,
        v.growth_perpetual,
        v.discount_rate,
        v.current_market_value ?? null,
        v.enterprise_value
      );
    // 裁剪：仅保留最近 100 条（按 id 倒序）
    const keep = db
      .prepare(`SELECT id FROM company_valuations ORDER BY id DESC LIMIT 100`)
      .all()
      .map((r) => r.id);
    if (keep.length) {
      const ph = keep.map(() => "?").join(",");
      db.prepare(`DELETE FROM company_valuations WHERE id NOT IN (${ph})`).run(...keep);
    }
    return r.lastInsertRowid;
  });
}

function listValuations(limit = 100) {
  const db = getDb();
  return db
    .prepare(
      `SELECT id, ${COLS}, created_at FROM company_valuations ORDER BY id DESC LIMIT ?`
    )
    .all(limit);
}

function deleteById(id) {
  const db = getDb();
  const r = db.prepare(`DELETE FROM company_valuations WHERE id=?`).run(id);
  return r.changes > 0;
}

function deleteAll() {
  const db = getDb();
  const r = db.prepare(`DELETE FROM company_valuations`).run();
  return r.changes;
}

module.exports = { insertValuation, listValuations, deleteById, deleteAll };
