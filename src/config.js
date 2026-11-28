"use strict";
/** 全局配置：全部来自环境变量，带合理默认。FunctionWeb 注入 PORT。*/
const path = require("path");

function bool(name, def) {
  const v = (process.env[name] || String(def)).toLowerCase();
  return v === "true" || v === "1" || v === "yes";
}
function int(name, def) {
  const v = parseInt(process.env[name], 10);
  return Number.isFinite(v) ? v : def;
}
function num(name, def) {
  const v = parseFloat(process.env[name]);
  return Number.isFinite(v) ? v : def;
}

const ROOT = path.resolve(__dirname, "..");
// 数据目录可通过 DATA_DIR 覆盖（FunctionWeb 上打包目录只读，运行时写入需指向 ${TMPDIR:-/tmp}）；
// 未显式设置 DB_PATH 时，数据库文件位于 dataDir/app.db。
const dataDir = path.resolve(process.env.DATA_DIR || path.join(ROOT, "data"));
const dbPath = process.env.DB_PATH
  ? path.resolve(process.env.DB_PATH)
  : path.join(dataDir, "app.db");

const config = {
  appName: process.env.APP_NAME || "EventDrive",
  version: process.env.APP_VERSION || "1.0.0",
  debug: bool("DEBUG", false),

  host: process.env.HOST || "0.0.0.0",
  port: int("PORT", 3000),

  dataDir,
  dbPath,

  staticDir: path.resolve(ROOT, "static"),
  viewsDir: path.resolve(__dirname, "views"),

  startScheduler: bool("START_SCHEDULER", true),

  // 抓取纪律
  fetchConcurrency: int("FETCH_CONCURRENCY", 5),
  fetchMinIntervalMs: int("FETCH_MIN_INTERVAL_MS", 350),
  fetchMaxRetries: int("FETCH_MAX_RETRIES", 3),
  fetchCooldownAfterFails: int("FETCH_COOLDOWN_AFTER_FAILS", 3),
  fetchCooldownMs: int("FETCH_COOLDOWN_MS", 6000),
  fetchTimeoutMs: int("FETCH_TIMEOUT_MS", 20000),
  fetchCacheTtlMs: int("FETCH_CACHE_TTL_MS", 10 * 60 * 1000),

  // 惰性刷新 TTL（小时）：超过则下次读触发后台刷新
  macroCacheTtlHours: num("MACRO_CACHE_TTL_HOURS", 12),
  stockTempCacheTtlHours: num("STOCKTEMP_CACHE_TTL_HOURS", 12),
  marketCacheTtlHours: num("MARKET_CACHE_TTL_HOURS", 12),

  newsPerSource: int("NEWS_PER_SOURCE", 20),
};

module.exports = config;
