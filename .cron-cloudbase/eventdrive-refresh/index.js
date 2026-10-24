/**
 * CloudBase 云函数：EventDrive 每日收盘抓取（北京时间 16:17）
 *
 * 与腾讯云 SCF 版的差异（三个）：
 * 1. 导出方式：CloudBase 用 exports.main，SCF 用 main_handler(event, context)
 * 2. 定时触发器 cron 是七段（首位为秒）：0 17 16 * * 1-5 *
 *    SCF 是六段：17 16 * * * *（无秒位）
 * 3. 环境变量在 CloudBase 控制台「函数配置 → 环境变量」配置
 *
 * 注意：函数运行时长上限通常 60~900 秒，**需在控制台把 Timeout 调到 900**；
 * 本函数按 900 秒设计，内部 deadline 取 800 秒（留 100 秒余量）。
 *
 * 2026-10-24 改造（原先是「点火 → 死等 700 秒 → 报告缺失」，两个毛病）：
 * a. 死等：一轮正常只要 350 秒左右，也要空等满 700 秒才出结果。
 *    → 改成**每 20 秒查一次 crawl_logs，四域齐了就提前结束**。
 * b. 只报告不补救：发现 stock_temp 缺失时只打一行 "需手动补跑"，从不真补。
 *    → 现在会**按缺失域自动补跑一次**（/api/crawl/daily-refresh?domains=xxx），
 *      并把「进程还在跑」和「进程已死」区分开报告。
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const TOKEN = process.env.EVENTDRIVE_SESSION_TOKEN || '';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';

// 快域：几十秒内落库。慢域：股市热度单独跑，实测 260 秒（被限流时更久）。
// **不含 news**（2026-10-24 起）：新闻只在上午 09:15 由 eventdrive-morning-news
// 抓一轮，收盘轮不再重复抓。若仍把它列在 NEED 里，本函数会一直等一个永远
// 不会落库的域，最后误判「缺失」并去补跑新闻。
const FAST = ['market', 'macro'];
const SLOW = ['stock_temp'];
const NEED = [...FAST, ...SLOW];

const POLL_MS = 20 * 1000;
const DEADLINE_MS = 800 * 1000; // 函数 Timeout 900s，留 100s 收尾
const START = Date.now();

function request(path, headers, method) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      BASE + path,
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

/** 读 crawl_logs，返回「今天（北京时间）已落库」的 task 集合。 */
async function loggedToday() {
  const r = await request(
    '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status' +
      '&order=crawl_time.desc&limit=60',
    { 'x-wb-webapp-access-key': KEY }
  );
  if (r.status !== 200) throw new Error('crawl_logs HTTP ' + r.status);
  const rows = JSON.parse(r.body);

  // CloudBase 运行时为 UTC；crawl_time 存的是 UTC naive，先补 Z 再转北京时间取日期
  const today = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const got = new Set();
  const detail = {};
  for (const row of rows) {
    const t = new Date(String(row.crawl_time).replace(' ', 'T') + 'Z');
    const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
    if (d !== today || got.has(row.task)) continue;
    got.add(row.task);
    detail[row.task] = {
      at: new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19),
      status: row.status,
      items: row.item_count,
    };
  }
  return { got, detail };
}

/** 点火；domains 为 null 时跑整轮。 */
async function fire(domains) {
  const q = domains && domains.length ? '?domains=' + domains.join(',') : '';
  const r = await request('/api/crawl/daily-refresh' + q, cookieHeaders(), 'POST');
  console.log(`[fire] ${domains ? domains.join(',') : '整轮'} -> HTTP ${r.status} ${r.body}`);
  return r;
}

function cookieHeaders() {
  return { Cookie: 'session_token=' + TOKEN };
}

/** 轮询直到 targets 全部落库 / 超时 / 预算用尽。 */
async function waitFor(targets, budgetMs) {
  const until = Date.now() + budgetMs;
  let last = { got: new Set(), detail: {} };
  while (Date.now() < until) {
    await sleep(POLL_MS);
    try {
      last = await loggedToday();
    } catch (e) {
      console.log(`[poll ${elapsed()}s] 查日志失败: ${e.message}`);
      continue;
    }
    const missing = targets.filter((t) => !last.got.has(t));
    console.log(
      `[poll ${elapsed()}s] 已落库 ${targets.length - missing.length}/${targets.length}` +
        (missing.length ? ` 缺: ${missing.join(',')}` : ' 齐了')
    );
    if (!missing.length) break;
  }
  return last;
}

exports.main = async (event, context) => {
  if (!TOKEN || !KEY) {
    return { ok: false, msg: '缺少环境变量 EVENTDRIVE_SESSION_TOKEN / EVENTDRIVE_CLOUD_DB_KEY' };
  }
  const out = { ok: false, step: 'start' };

  // 1) 点火整轮（接口立即返回，抓取在沙箱后台线程跑）
  try {
    const r = await fire(null);
    if (r.status !== 200 && r.status !== 409) {
      throw new Error('HTTP ' + r.status + ' ' + String(r.body).slice(0, 200));
    }
    out.ignite = r.body;
    out.step = 'ignited';
  } catch (e) {
    console.log('[1] 点火失败:', e.message);
    return { ok: false, step: 'ignite_failed', error: String(e.message).slice(0, 300) };
  }

  // 2) 自适应等待：四域齐了就提前收工，不再死等固定时长
  let state = await waitFor(NEED, DEADLINE_MS - (Date.now() - START));
  let missing = NEED.filter((t) => !state.got.has(t));
  console.log(
    `[2] 首轮结束 @${elapsed()}s，缺: ${missing.join(',') || '无'}`
  );

  // 3) 仍有缺失且预算够 -> 按域补跑一次（这正是原先「只报告不补救」缺的那步）
  let retried = [];
  if (missing.length) {
    const left = DEADLINE_MS - (Date.now() - START);
    if (left > 60 * 1000) {
      retried = missing.slice();
      console.log(`[3] 补跑缺失域: ${retried.join(',')}（剩余预算 ${Math.round(left / 1000)}s）`);
      try {
        const r = await fire(retried);
        // 409 = 该域仍在跑（进程活着，只是慢），不该再点火
        if (r.status === 200) {
          state = await waitFor(retried, left - 10 * 1000);
        } else {
          console.log('[3] 补跑被拒（该域仍在执行），跳过等待');
        }
      } catch (e) {
        console.log('[3] 补跑点火失败:', e.message);
      }
      missing = NEED.filter((t) => !state.got.has(t));
    } else {
      console.log('[3] 预算不足，跳过补跑');
    }
  }

  // 4) 判断「进程还活着但慢」还是「进程已经没了」
  let status = {};
  try {
    const r = await request('/api/crawl/daily-status', cookieHeaders());
    status = JSON.parse(r.body);
  } catch (e) {
    console.log('[4] 查 daily-status 失败:', e.message);
  }
  const stillRunning = Array.isArray(status.domains) ? status.domains : [];

  const okList = NEED.filter((t) => state.got.has(t));
  const hint = missing.length
    ? '缺失域: ' + missing.join(',') +
      (stillRunning.length
        ? `（进程仍在执行 ${stillRunning.join(',')}，可能是耗时过长，非崩溃）`
        : '（进程已无在执行域，疑似被沙箱回收，建议手动补跑）') +
      '。手动补跑: POST /api/crawl/daily-refresh?domains=' + missing.join(',')
    : '全部落库';

  console.log(`[4] 完成 @${elapsed()}s ok=${okList.join(',')} miss=${missing.join(',')}`);
  return {
    ok: missing.length === 0,
    step: 'done',
    elapsed_s: elapsed(),
    ok_domains: okList,
    missing,
    retried,
    still_running: stillRunning,
    detail: state.detail,
    hint,
  };
};
