"use strict";
/** /health 数据源状态聚合：每张表的最后落库时间、年龄、是否过期、记录数。
 *  只读，不触发抓取；过期判定与各服务 freshness() 口径一致。*/
const { getDb } = require("../db");
const scheduler = require("../scheduler");

function ageHours(utcStr) {
  if (!utcStr) return null;
  const ts = Date.parse(String(utcStr).replace(" ", "T") + "Z");
  if (!ts || Number.isNaN(ts)) return null;
  return Math.round(((Date.now() - ts) / 3600000) * 10) / 10;
}
function cnStr(ts) {
  if (!ts) return "—";
  const d = new Date(String(ts).endsWith("Z") ? ts : String(ts).replace(" ", "T") + "Z");
  if (isNaN(d.getTime())) return String(ts);
  return new Date(d.getTime() + 8 * 3600 * 1000).toISOString().replace("T", " ").slice(0, 19);
}
function scalar(sql) {
  try {
    const row = getDb().prepare(sql).get();
    return row ? Object.values(row)[0] : null;
  } catch (_) {
    return null;
  }
}
function source(name, table, ttlHours) {
  const last = scalar(`SELECT MAX(updated_at) AS t FROM ${table}`);
  const records = scalar(`SELECT COUNT(*) AS c FROM ${table}`);
  const age = ageHours(last);
  return {
    name,
    last_updated: cnStr(last),
    age_hours: age,
    stale: age != null && age > ttlHours,
    records: records || 0,
  };
}

function getHealth() {
  return {
    sources: [
      source("news", "news", 26),
      source("market", "market_prices", 26),
      source("macro", "macro_indicators", 26),
      source("stock_temp", "stock_temp_indicators", 30),
    ],
    scheduler: { running: scheduler.isRunning(), jobs: scheduler.listJobs() },
  };
}

module.exports = { getHealth };
