"use strict";
/** 调度器：node-cron，全部按北京时间（Asia/Shanghai）错峰触发。
 *
 *  周一至周五：
 *    08:13  新闻抓取（东方财富）
 *    21:34  全球行情刷新
 *    21:36  宏观指标刷新
 *    21:38  股市温度刷新
 *
 *  无鉴权、无历史回溯；手动触发走 /api/crawl/trigger。
 *  任务失败只记日志、保留上次成功读数，绝不写兜底值。
 */
const cron = require("node-cron");

const TZ = "Asia/Shanghai";
const MAX_LOGS = 80;

let running = false;
const tasks = [];
const logs = [];

function nowCST() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().replace("T", " ").slice(0, 19);
}
function log(msg) {
  const line = { time: nowCST().slice(11, 19), message: String(msg) };
  logs.push(line);
  if (logs.length > MAX_LOGS) logs.shift();
  console.log("[scheduler] " + line.time + " " + line.message);
}

/** 任务定义（延迟 require 服务，避免潜在循环依赖）。*/
function defineTasks() {
  const newsSvc = require("./services/newsService");
  const marketSvc = require("./services/marketService");
  const macroSvc = require("./services/macroService");
  const stockSvc = require("./services/stockTempService");
  return [
    { id: "news", name: "新闻抓取（东方财富）", cron: "13 8 * * 1-5", handler: () => newsSvc.fetchAndStore() },
    { id: "market", name: "全球行情刷新", cron: "34 21 * * 1-5", handler: () => marketSvc.fetchAndStore() },
    { id: "macro", name: "宏观指标刷新", cron: "36 21 * * 1-5", handler: () => macroSvc.fetchAndStore() },
    { id: "stock-temp", name: "股市温度刷新", cron: "38 21 * * 1-5", handler: () => stockSvc.fetchAndStore() },
  ];
}

function wrap(task, handler) {
  return async () => {
    if (task.running) {
      log(`⏭ ${task.name} 仍在执行，跳过本次触发`);
      return;
    }
    task.running = true;
    log(`🚀 ${task.name} 开始执行`);
    try {
      await handler();
      task.lastRun = nowCST();
      task.lastError = null;
      log(`✨ ${task.name} 完成`);
    } catch (e) {
      task.lastError = String((e && e.message) || e);
      log(`❌ ${task.name} 失败: ${task.lastError}`);
    } finally {
      task.running = false;
    }
  };
}

function start() {
  if (running) return;
  running = true;
  for (const def of defineTasks()) {
    const task = { id: def.id, name: def.name, cron: def.cron, running: false, lastRun: null, lastError: null, job: null };
    task.handler = wrap(task, def.handler);
    try {
      task.job = cron.schedule(def.cron, task.handler, { timezone: TZ, scheduled: true });
      tasks.push(task);
      log(`⏰ 已注册: ${task.name}（${def.cron} ${TZ}）`);
    } catch (e) {
      log(`⚠️ 注册失败 ${task.name}: ${e.message}`);
    }
  }
  log(`调度器已启动（${tasks.length} 个任务，时区 ${TZ}）`);
}

function stop() {
  for (const t of tasks) {
    try { t.job && t.job.stop(); } catch (_) { /* ignore */ }
  }
  running = false;
  log("调度器已停止");
}

function isRunning() { return running; }

function listJobs() {
  return tasks.map((t) => ({
    id: t.id, name: t.name, cron: t.cron, timezone: TZ,
    running: t.running, last_run: t.lastRun, last_error: t.lastError,
  }));
}

function status() {
  return { is_running: running, timezone: TZ, jobs: listJobs(), logs: logs.slice() };
}

function getLogs() { return logs.slice(); }

/** 手动触发：id 为任务 id 或 "all"。立即返回触发状态，不等待完成。*/
function trigger(id) {
  const targets = id && id !== "all" ? tasks.filter((t) => t.id === id) : tasks;
  if (!targets.length) throw new Error("未知任务: " + id);
  const results = [];
  for (const t of targets) {
    if (t.running) { results.push({ id: t.id, status: "already_running" }); continue; }
    results.push({ id: t.id, status: "started" });
    t.handler();
  }
  return { status: "started", triggered: results };
}

module.exports = { start, stop, isRunning, listJobs, status, getLogs, trigger, _tasks: tasks };
