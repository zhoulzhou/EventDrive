/**
 * CloudBase 云函数：主题资金快照（北京时间 22:10，周一至周五）
 *
 * 为什么要独立成函数（2026-11-22）：
 * 主题资金原先排在 eventdrive-refresh 收盘轮的最后一步，它要抓 50 只成分股
 * 的历史资金流（40+ 次 HTTP + 限流间隔），单跑约 40~50 秒。
 * 收盘轮里前面的 stock_temp / hotspot 已把进程时间吃掉一大截，主题资金经常
 * 跑到一半就被沙箱回收——连 failed 日志都来不及写，日志里表现为「从未执行」。
 * 11-18~20 连续三天因此缺数。拆出来单独点火、独立进程、独立时间窗，
 * 不再跟收盘轮挤在一起，也不受那个函数 800 秒 deadline 的收尾调度影响。
 *
 * 运行时长：函数 Timeout 300 秒（单跑约 40~50 秒，留足余量）。
 * cron 七段（首位秒）：0 10 22 * * 1-5 * = 周一至周五 22:10:00。
 *
 * 环境变量与 eventdrive-refresh 完全相同（同账号同环境）：
 *   EVENTDRIVE_SESSION_TOKEN / EVENTDRIVE_CLOUD_DB_KEY
 * sharecode 只对 /api/ 路径追加（云库 REST 路径带参数会被 PostgREST 当 filter）。
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const TOKEN = process.env.EVENTDRIVE_SESSION_TOKEN || '';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';
const SHARECODE = process.env.EVENTDRIVE_SHARECODE || 'X6GXVFI6UH';

const DOMAIN = 'theme_capital';
const POLL_MS = 15 * 1000;
const DEADLINE_MS = 260 * 1000; // Timeout 300s，留 40s 收尾
const START = Date.now();

function urlFor(path) {
  if (path.indexOf('/api/') !== 0) return BASE + path;
  return BASE + path + (path.indexOf('?') >= 0 ? '&' : '?') + 'sharecode=' + SHARECODE;
}

function request(path, headers, method) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      urlFor(path),
      { method: method || 'GET', headers, timeout: 40000 },
      (res) => {
        let data = '';
        res.on('data', (c) => (data += c));
        res.on('end', () => resolve({ status: res.statusCode, body: data }));
      }
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const elapsed = () => Math.round((Date.now() - START) / 1000);

function cookieHeaders() {
  return { Cookie: 'session_token=' + TOKEN };
}

/** 北京时间 HH:MM:SS（crawl_time 存 UTC naive，补 Z 后加 8h）。 */
function bjtClock(ms) {
  return new Date((ms || Date.now()) + 8 * 3600 * 1000).toISOString().slice(11, 19);
}

/**
 * 点火：非 200/409 按 15 秒间隔重试共 3 次（沙箱冷启动首请求常超 40 秒超时）。
 * 409 表示该域正在跑，视为正常。
 */
async function fire() {
  const path = '/api/crawl/daily-refresh?domains=' + DOMAIN;
  let lastErr = null;
  for (let i = 1; i <= 3; i++) {
    try {
      const r = await request(path, cookieHeaders(), 'POST');
      if (r.status === 200 || r.status === 409) {
        console.log(`[fire] 第 ${i} 次 HTTP ${r.status}: ${String(r.body).slice(0, 200)}`);
        return r;
      }
      lastErr = new Error('HTTP ' + r.status + ' ' + String(r.body).slice(0, 120));
    } catch (e) {
      lastErr = e;
    }
    console.log(`[fire] 第 ${i}/3 次失败: ${lastErr.message}`);
    if (i < 3) await sleep(15 * 1000);
  }
  throw lastErr || new Error('点火失败');
}

/** 读 crawl_logs，判断今天（北京时间）主题资金是否已落库。 */
async function loggedToday() {
  const r = await request(
    '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status' +
      '&order=crawl_time.desc&limit=60',
    { 'x-wb-webapp-access-key': KEY }
  );
  if (r.status !== 200) throw new Error('crawl_logs HTTP ' + r.status);
  const rows = JSON.parse(r.body);
  const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  for (const row of rows) {
    if (row.task !== DOMAIN) continue;
    const t = new Date(String(row.crawl_time).replace(' ', 'T') + 'Z');
    const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
    if (d !== today) continue;
    if (row.status === 'ok' || row.status === 'empty') {
      return {
        landed: true,
        at: new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19),
        status: row.status,
        items: row.item_count,
      };
    }
    // 今天已有 failed 行：终局，不必再等
    if (row.status === 'failed') {
      return {
        landed: false,
        failed: true,
        at: new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19),
      };
    }
  }
  return { landed: false };
}

exports.main = async () => {
  if (!TOKEN || !KEY) {
    return { ok: false, msg: '缺少环境变量 EVENTDRIVE_SESSION_TOKEN / EVENTDRIVE_CLOUD_DB_KEY' };
  }
  const out = { ok: false, step: 'start' };

  // 1) 点火单域
  let igniteAt = bjtClock();
  try {
    const r = await fire();
    igniteAt = bjtClock();
    out.ignite = r.body;
    out.step = 'ignited';
  } catch (e) {
    console.log('[1] 点火失败:', e.message);
    return { ok: false, step: 'ignite_failed', error: String(e.message).slice(0, 300) };
  }

  // 2) 轮询等落库（15s 一次，直到 deadline）
  let state = { landed: false };
  const until = Date.now() + (DEADLINE_MS - (Date.now() - START));
  while (Date.now() < until) {
    await sleep(POLL_MS);
    try {
      state = await loggedToday();
    } catch (e) {
      console.log(`[poll ${elapsed()}s] 查日志失败: ${e.message}`);
      continue;
    }
    console.log(`[poll ${elapsed()}s] ${state.landed ? '已落库' : state.failed ? '失败' : '等待中'}`);
    if (state.landed || state.failed) break;
  }

  out.elapsed_s = elapsed();
  out.theme_capital = state;
  out.step = 'done';
  out.ok = Boolean(state.landed);
  if (!state.landed) {
    out.hint = state.failed
      ? '主题资金本轮已写 failed 日志，请查看 crawl_logs 的 error_message'
      : '轮询到 deadline 仍未落库，可能是空数据(empty)或进程被回收；手动补跑: '
        + 'POST /api/crawl/daily-refresh?domains=theme_capital';
  }
  console.log(`[4] 完成 @${out.elapsed_s}s ok=${out.ok}`, state);
  return out;
};
