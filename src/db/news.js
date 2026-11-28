"use strict";
/** 新闻 repo：按 url 去重 upsert、分页列表（含 24h  cutoff 可选）、详情。*/
const { getDb } = require("./index");

function upsertNews(rec) {
  const db = getDb();
  const existing = db.prepare("SELECT id FROM news WHERE url=?").get(rec.url);
  if (existing) {
    db.prepare(
      `UPDATE news SET title=?, content=?, source=?, publish_time=?, author=?, summary=?, image_path=?, updated_at=datetime('now') WHERE id=?`
    ).run(
      rec.title,
      rec.content ?? null,
      rec.source,
      rec.publish_time,
      rec.author ?? null,
      rec.summary ?? null,
      rec.image_path ?? null,
      existing.id
    );
    return { id: existing.id, inserted: false };
  }
  const r = db
    .prepare(
      `INSERT INTO news (title, content, source, publish_time, url, author, summary, image_path)
       VALUES (?,?,?,?,?,?,?,?)`
    )
    .run(
      rec.title,
      rec.content ?? null,
      rec.source,
      rec.publish_time,
      rec.url,
      rec.author ?? null,
      rec.summary ?? null,
      rec.image_path ?? null
    );
  return { id: r.lastInsertRowid, inserted: true };
}

/**
 * 列表查询。opts: { skip, limit, source, sinceHours, include_keywords, exclude_keywords }
 * sinceHours：只返回该小时数内的新闻（对齐原 24h 窗口）；不传/0 则不限。
 */
function listNews(opts = {}) {
  const { skip = 0, limit = 100, source, sinceHours, include_keywords, exclude_keywords } = opts;
  const db = getDb();
  const conds = [];
  const params = [];
  if (sinceHours && sinceHours > 0) {
    conds.push("publish_time >= datetime('now', ?)");
    params.push(`-${sinceHours} hours`);
  }
  if (source) {
    conds.push("source = ?");
    params.push(source);
  }
  if (include_keywords && include_keywords.length) {
    const ors = [];
    for (const kw of include_keywords) {
      const k = (kw || "").trim();
      if (!k) continue;
      ors.push("(title LIKE ? OR content LIKE ?)");
      params.push(`%${k}%`, `%${k}%`);
    }
    if (ors.length) conds.push(`(${ors.join(" OR ")})`);
  }
  if (exclude_keywords && exclude_keywords.length) {
    for (const kw of exclude_keywords) {
      const k = (kw || "").trim();
      if (!k) continue;
      conds.push("title NOT LIKE ? AND content NOT LIKE ?");
      params.push(`%${k}%`, `%${k}%`);
    }
  }
  const where = conds.length ? "WHERE " + conds.join(" AND ") : "";
  params.push(limit, skip);
  return db
    .prepare(
      `SELECT id, title, source, publish_time, url, summary FROM news ${where}
       ORDER BY publish_time DESC, id DESC LIMIT ? OFFSET ?`
    )
    .all(...params);
}

function getNewsById(id) {
  const db = getDb();
  return db
    .prepare(
      `SELECT id, title, content, source, publish_time, url, author, summary, image_path, created_at FROM news WHERE id=?`
    )
    .get(id);
}

function countNews() {
  const db = getDb();
  const r = db.prepare("SELECT COUNT(*) AS c FROM news").get();
  return r ? r.c : 0;
}

module.exports = { upsertNews, listNews, getNewsById, countNews };
