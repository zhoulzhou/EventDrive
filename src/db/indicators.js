"use strict";
/**
 * 泛型指标 repo：宏观指标(macro_indicators / macro_indicator_history) 与
 * 股市温度(stock_temp_indicators / stock_temp_history) 表结构一致，共用此实现。
 *
 * 口径对齐原 app/crud.py：
 *  - 快照表 upsert：读数不变也刷 updated_at（页面据此判断调度器存活）。
 *  - 历史表按 (key, as_of) 定位：不存在→插入；同值→跳过；不同值→就地修正。
 *  - 不写任何 fallback / seed（AC-12）。
 */
const { getDb } = require("./index");

function parseDetail(row) {
  if (!row) return row;
  if (row.detail != null && typeof row.detail === "string") {
    try {
      row.detail = JSON.parse(row.detail);
    } catch (_) {
      /* 保持原字符串 */
    }
  }
  return row;
}

function sameValue(a, b) {
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  const na = Number(a), nb = Number(b);
  if (!Number.isFinite(na) || !Number.isFinite(nb)) return String(a) === String(b);
  return Math.abs(na - nb) < 1e-9;
}

function makeIndicatorRepo(latestTbl, histTbl) {
  function upsertLatest(records) {
    if (!records || !records.length) return 0;
    const db = getDb();
    return db.transaction(() => {
      const stmt = db.prepare(
        `INSERT INTO ${latestTbl} (key, value, as_of, source, detail) VALUES (?,?,?,?,?)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value, as_of=excluded.as_of,
           source=excluded.source, detail=excluded.detail, updated_at=datetime('now')`
      );
      let n = 0;
      for (const rec of records) {
        const detail = rec.detail != null ? JSON.stringify(rec.detail) : null;
        stmt.run(rec.key, rec.value ?? null, rec.as_of ?? null, rec.source || "fetcher", detail);
        n++;
      }
      return n;
    });
  }

  function getAllLatest() {
    const db = getDb();
    return db
      .prepare(`SELECT key, value, as_of, source, detail, updated_at FROM ${latestTbl}`)
      .all()
      .map(parseDetail);
  }

  function getLatest(key) {
    const db = getDb();
    return parseDetail(
      db
        .prepare(
          `SELECT key, value, as_of, source, detail, updated_at FROM ${latestTbl} WHERE key=?`
        )
        .get(key)
    );
  }

  function getLastUpdated(source) {
    const db = getDb();
    const row = source
      ? db.prepare(`SELECT MAX(updated_at) AS t FROM ${latestTbl} WHERE source=?`).get(source)
      : db.prepare(`SELECT MAX(updated_at) AS t FROM ${latestTbl}`).get();
    return row ? row.t : null;
  }

  function bulkUpsertHistory(records) {
    if (!records || !records.length) return { added: 0, updated: 0 };
    const db = getDb();
    return db.transaction(() => {
      const keys = [...new Set(records.map((r) => r.key))];
      const ph = keys.map(() => "?").join(",");
      const existingRows = db
        .prepare(`SELECT id, key, value, as_of FROM ${histTbl} WHERE key IN (${ph})`)
        .all(...keys);
      const existing = new Map();
      for (const r of existingRows) existing.set(r.key + "|" + (r.as_of || ""), r);

      const ins = db.prepare(
        `INSERT INTO ${histTbl} (key, value, as_of, source, detail) VALUES (?,?,?,?,?)`
      );
      const upd = db.prepare(`UPDATE ${histTbl} SET value=?, source=?, detail=? WHERE id=?`);
      let added = 0, updated = 0;
      for (const rec of records) {
        const k = rec.key + "|" + (rec.as_of || "");
        const row = existing.get(k);
        const detail = rec.detail != null ? JSON.stringify(rec.detail) : null;
        const val = rec.value ?? null;
        if (!row) {
          const res = ins.run(rec.key, val, rec.as_of ?? null, rec.source || "fetcher", detail);
          existing.set(k, { id: res.lastInsertRowid, key: rec.key, value: val, as_of: rec.as_of });
          added++;
        } else if (sameValue(row.value, val)) {
          // 同值跳过
        } else {
          upd.run(val, rec.source || "fetcher", detail, row.id);
          row.value = val;
          updated++;
        }
      }
      return { added, updated };
    });
  }

  function getHistorySeries(keys, perKeyLimit = 3000) {
    const result = {};
    keys.forEach((k) => (result[k] = []));
    if (!keys.length) return result;
    const db = getDb();
    const ph = keys.map(() => "?").join(",");
    const rows = db
      .prepare(
        `SELECT h.key, h.value, h.as_of, h.source, h.detail, h.fetched_at
         FROM ${histTbl} h
         JOIN (
           SELECT id, ROW_NUMBER() OVER (PARTITION BY key ORDER BY as_of DESC, id DESC) AS rn
           FROM ${histTbl} WHERE key IN (${ph})
         ) x ON h.id = x.id
         WHERE x.rn <= ?`
      )
      .all(...keys, perKeyLimit)
      .map(parseDetail);
    const grouped = {};
    for (const r of rows) (grouped[r.key] = grouped[r.key] || []).push(r);
    for (const k of keys) {
      const grp = grouped[k] || [];
      grp.sort((a, b) => (a.as_of || "").localeCompare(b.as_of || "") || a.id - b.id);
      result[k] = grp;
    }
    return result;
  }

  function getHistory(key, limit = 200, order = "desc") {
    const db = getDb();
    const dir = order === "asc" ? "ASC" : "DESC";
    if (key) {
      return db
        .prepare(
          `SELECT key, value, as_of, source, detail, fetched_at FROM ${histTbl} WHERE key=? ORDER BY id ${dir} LIMIT ?`
        )
        .all(key, limit)
        .map(parseDetail);
    }
    return db
      .prepare(
        `SELECT key, value, as_of, source, detail, fetched_at FROM ${histTbl} ORDER BY id ${dir} LIMIT ?`
      )
      .all(limit)
      .map(parseDetail);
  }

  function countHistory(key) {
    const db = getDb();
    const row = key
      ? db.prepare(`SELECT COUNT(*) AS c FROM ${histTbl} WHERE key=?`).get(key)
      : db.prepare(`SELECT COUNT(*) AS c FROM ${histTbl}`).get();
    return row ? row.c : 0;
  }

  return {
    upsertLatest,
    getAllLatest,
    getLatest,
    getLastUpdated,
    bulkUpsertHistory,
    getHistorySeries,
    getHistory,
    countHistory,
  };
}

const macroRepo = makeIndicatorRepo("macro_indicators", "macro_indicator_history");
const stockTempRepo = makeIndicatorRepo("stock_temp_indicators", "stock_temp_history");

module.exports = { makeIndicatorRepo, macroRepo, stockTempRepo };
