"use strict";
/**
 * 共享 HTTP 抓取客户端（Node 内置 https/http，无第三方依赖）。
 *
 * 纪律（来自探针验证 + 经验 2288163/2207209）：
 *   - 全局并发上限（默认 5）
 *   - 两次请求发起最小间隔（默认 350ms + 抖动）
 *   - 失败指数退避 + 抖动（600 → 1200ms），单端点重试 ≤3
 *   - 按主机连续失败 ≥3 触发该主机冷却（默认 6s），冷却期间该主机不发新请求
 *   - 内存缓存响应（按 URL，TTL 默认 10 分钟）
 *   - 正确 UA + Referer（由 sources.js 注入）
 *   - 支持 gzip/deflate 解压
 */
const https = require("https");
const http = require("http");
const zlib = require("zlib");
const { URL } = require("url");
const config = require("../config");

const cache = new Map(); // url -> { status, headers, body, ts }

// 按主机维护失败计数与冷却，避免一个坏源拖垮其他源
const hostState = new Map();

let nextDispatchAt = 0;
let active = 0;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (ms) => Math.floor(ms * (0.7 + Math.random() * 0.6));

function getHostState(host) {
  let s = hostState.get(host);
  if (!s) {
    s = { consecutiveFails: 0, cooldownUntil: 0 };
    hostState.set(host, s);
  }
  return s;
}

function bumpFail(hs, host) {
  hs.consecutiveFails++;
  if (hs.consecutiveFails >= config.fetchCooldownAfterFails) {
    hs.cooldownUntil = Date.now() + config.fetchCooldownMs;
    hs.consecutiveFails = 0;
    console.warn(
      `[fetch] 主机 ${host} 连续失败达阈值 → 冷却 ${config.fetchCooldownMs}ms`
    );
  }
}

// 并发信号量：限制同时在途的请求数
const slotWaiters = [];
function acquireSlot() {
  return new Promise((resolve) => {
    const tryAcquire = () => {
      if (active < config.fetchConcurrency) {
        active++;
        resolve();
      } else {
        slotWaiters.push(tryAcquire);
      }
    };
    tryAcquire();
  });
}
function releaseSlot() {
  active = Math.max(0, active - 1);
  const next = slotWaiters.shift();
  if (next) next();
}

// 串行派发门：保证任意两次请求发起之间的间隔 ≥ minInterval（避免多个等待者同时唤醒）
let gateChain = Promise.resolve();
function passGate() {
  const p = gateChain.then(async () => {
    const now = Date.now();
    if (now < nextDispatchAt) await sleep(nextDispatchAt - now);
    nextDispatchAt = Date.now() + config.fetchMinIntervalMs + jitter(120);
  });
  gateChain = p.catch(() => {});
  return p;
}

function rawGet(urlStr, headers, raw) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const lib = u.protocol === "http:" ? http : https;
    const req = lib.request(
      {
        method: "GET",
        hostname: u.hostname,
        port: u.port || (u.protocol === "http:" ? 80 : 443),
        path: u.pathname + u.search,
        headers: { ...headers, "Accept-Encoding": "gzip, deflate" },
      },
      (res) => {
        const chunks = [];
        let stream = res;
        const enc = (res.headers["content-encoding"] || "").toLowerCase();
        if (enc.includes("gzip")) stream = res.pipe(zlib.createGunzip());
        else if (enc.includes("deflate")) stream = res.pipe(zlib.createInflate());
        stream.on("data", (c) => chunks.push(c));
        stream.on("end", () => {
          const buf = Buffer.concat(chunks);
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: raw ? buf : buf.toString("utf8"),
          });
        });
        stream.on("error", reject);
      }
    );
    req.on("error", reject);
    req.setTimeout(config.fetchTimeoutMs, () =>
      req.destroy(new Error("timeout"))
    );
    req.end();
  });
}

/**
 * GET 请求，自动重试/退避/冷却/缓存。
 * @param {string} urlStr
 * @param {object} headers
 * @param {object} [opts] { noCache: boolean, raw: boolean }
 *   raw=true 时 body 返回 Buffer（用于 GBK 等非 UTF-8 页面自行解码）
 * @returns {Promise<{status:number, headers:object, body:string|Buffer, fromCache?:boolean}>}
 * @throws 最终失败时抛错（调用方 try/catch，失败指标不入库）
 */
async function get(urlStr, headers = {}, opts = {}) {
  if (!opts.noCache) {
    const cached = cache.get(urlStr);
    if (cached && Date.now() - cached.ts < config.fetchCacheTtlMs) {
      return { ...cached, fromCache: true };
    }
  }

  const u = new URL(urlStr);
  const host = u.hostname;
  const hs = getHostState(host);

  if (Date.now() < hs.cooldownUntil) {
    const wait = hs.cooldownUntil - Date.now();
    console.warn(`[fetch] 主机 ${host} 冷却中，等待 ${wait}ms`);
    await sleep(wait);
  }

  let lastErr;
  for (let attempt = 0; attempt <= config.fetchMaxRetries; attempt++) {
    if (attempt > 0) await sleep(jitter(600 * Math.pow(2, attempt - 1)));
    await acquireSlot();
    await passGate();
    let r;
    try {
      r = await rawGet(urlStr, headers, opts.raw);
    } catch (e) {
      releaseSlot();
      lastErr = e;
      bumpFail(hs, host);
      continue;
    }
    releaseSlot();

    if (r.status >= 200 && r.status < 300) {
      hs.consecutiveFails = 0;
      if (!opts.noCache) {
        cache.set(urlStr, {
          status: r.status,
          headers: r.headers,
          body: r.body,
          ts: Date.now(),
        });
      }
      return r;
    }
    if (r.status >= 400 && r.status < 500) {
      // 4xx 多为参数/鉴权问题，重试无益
      bumpFail(hs, host);
      return r;
    }
    lastErr = new Error(`HTTP ${r.status}`);
    bumpFail(hs, host);
  }
  throw lastErr || new Error("fetch failed");
}

function clearCache() {
  cache.clear();
}
function getStats() {
  return {
    cacheSize: cache.size,
    active,
    hosts: hostState.size,
  };
}

module.exports = { get, clearCache, getStats };
