"use strict";
/**
 * 存储层：sql.js（WASM SQLite，纯 JS，无原生编译，Node 20/26 通用）。
 * 暴露类似 better-sqlite3 的 prepare().get/all/run 接口，便于业务代码编写。
 * 写入后落盘到 config.dbPath（实例级持久化；实例重启/重部署不保证保留，符合需求）。
 */
const fs = require("fs");
const path = require("path");
const initSqlJs = require("sql.js");
const config = require("../config");
const { initSchema } = require("./schema");

let _db = null;
let _saveTimer = null;

// prepared statement 可复用（循环内多次 run/get/all），由 GC 兜底释放底层 WASM 语句，避免泄漏
const _stmtRegistry = new FinalizationRegistry((rawStmt) => {
  try { rawStmt.free(); } catch (_) {}
});

function locateWasm(file) {
  // sql.js 包内 dist 目录
  return path.join(path.dirname(require.resolve("sql.js")), file);
}

class Stmt {
  constructor(wrap, db, sql) {
    this._wrap = wrap;
    this._db = db;
    this._sql = sql;
    this._stmt = db.prepare(sql);
    _stmtRegistry.register(this, this._stmt);
  }
  _bind(params) {
    if (params == null) return;
    // sql.js bind 接受数组或对象；统一转数组
    if (Array.isArray(params)) this._stmt.bind(params);
    else this._stmt.bind([params]);
  }
  get(...params) {
    this._stmt.reset();
    this._bind(params.length ? params : null);
    let row = null;
    if (this._stmt.step()) row = this._stmt.getAsObject();
    return row;
  }
  all(...params) {
    this._stmt.reset();
    this._bind(params.length ? params : null);
    const rows = [];
    while (this._stmt.step()) rows.push(this._stmt.getAsObject());
    return rows;
  }
  run(...params) {
    this._stmt.reset();
    this._bind(params.length ? params : null);
    this._stmt.step();
    const changes = this._db.getRowsModified();
    let lastInsertRowid = null;
    try {
      const r = this._db.exec("SELECT last_insert_rowid() AS id");
      if (r[0] && r[0].values[0]) lastInsertRowid = r[0].values[0][0];
    } catch (_) {}
    if (changes > 0 && this._wrap) this._wrap.scheduleSave();
    return { changes, lastInsertRowid };
  }
}

class DbWrap {
  constructor(db) {
    this._db = db;
  }
  prepare(sql) {
    return new Stmt(this, this._db, sql);
  }
  exec(sql) {
    return this._db.exec(sql);
  }
  transaction(fn) {
    this._db.run("BEGIN");
    try {
      const res = fn();
      this._db.run("COMMIT");
      this.scheduleSave();
      return res;
    } catch (e) {
      this._db.run("ROLLBACK");
      throw e;
    }
  }
  run(sql, params) {
    this._db.run(sql, params || []);
    this.scheduleSave();
    return { changes: this._db.getRowsModified() };
  }
  scheduleSave() {
    // 防抖落盘：多次连续写合并为一次磁盘写
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => this.save(), 400);
  }
  save() {
    if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }
    try {
      const data = this._db.export();
      fs.writeFileSync(config.dbPath, Buffer.from(data));
    } catch (e) {
      console.error("[db] 落盘失败:", e.message);
    }
  }
  close() {
    this.save();
    this._db.close();
  }
}

async function openDb() {
  if (_db) return _db;
  const SQL = await initSqlJs({ locateFile: locateWasm });
  fs.mkdirSync(config.dataDir, { recursive: true });
  let buffer = null;
  if (fs.existsSync(config.dbPath)) {
    try { buffer = fs.readFileSync(config.dbPath); } catch (_) { buffer = null; }
  }
  const raw = buffer ? new SQL.Database(buffer) : new SQL.Database();
  const wrap = new DbWrap(raw);
  initSchema(wrap);
  wrap.save(); // 建表后立即落盘，确保 data/app.db 存在
  _db = wrap;
  return _db;
}

function getDb() {
  if (!_db) throw new Error("数据库尚未初始化，请先 await openDb()");
  return _db;
}

module.exports = { openDb, getDb };
