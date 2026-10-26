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
 *    → 改成**每 20 秒查一次 crawl_logs，三域齐了就提前结束**。
 * b. 只报告不补救：发现 stock_temp 缺失时只打一行 "需手动补跑"，从不真补。
 *    → 现在会**按缺失域自动补跑一次**（/api/crawl/daily-refresh?domains=xxx），
 *      并把「进程还在跑」和「进程已死」区分开报告。
 *
 * 2026-10-26 改造（10-26 16:17 整轮零落库，事后无法判断「没触发」还是「触发但失败」）：
 * a. **点火加重试**：非 200/409 按 15 秒间隔重试共 3 次（沙箱冷启动首个请求常超过
 *    本函数 40 秒的单请求超时，一次失败就 return 是当时最可能的死因）。
 * b. **判断「已落库」只认 status=ok/empty**：这天起域失败也会往 crawl_logs 写
 *    status=failed，若仍把 failed 当落库，会在域失败时误判「齐了」而跳过补跑。
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

/** 北京时间 HH:MM:SS（用于和 crawl_logs 里的 at 字段做同日比较）。 */
function bjtClock(ms) {
  return new Date((ms || Date.now()) + 8 * 3600 * 1000).toISOString().slice(11, 19);
}

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
  const failedToday = {};
  for (const row of rows) {
    const t = new Date(String(row.crawl_time).replace(' ', 'T') + 'Z');
    const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
    if (d !== today || got.has(row.task)) continue;
    // **只有 ok / empty 才算「已落库」**（2026-10-26）：
    // 这一天起域失败也会往 crawl_logs 写 status=failed（以及 skipped），
    // 若仍把它们当作落库，本函数会在域失败时误判「齐了」而跳过补跑。
    // 注意顺序是 crawl_time desc：若「新的失败、旧的成功」，旧的 ok 行仍会把该域
    // 标成落库 —— 今天成功过一次就算跑过了，这是对的。
    if (row.status !== 'ok' && row.status !== 'empty') {
      if (row.status === 'failed' && !failedToday[row.task]) {
        failedToday[row.task] = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19);
      }
      continue;
    }
    got.add(row.task);
    detail[row.task] = {
      at: new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19),
      status: row.status,
      items: row.item_count,
    };
  }
  return { got, detail, failedToday };
}

const IGNITE_TRIES = 3;
const IGNITE_GAP_MS = 15 * 1000;

/**
 * 点火；domains 为 null 时跑整轮。
 *
 * 2026-10-26 加**重试**：沙箱空闲后会休眠/冷启动，首个请求可能要 30~60 秒才通，
 * 而本函数的单请求超时是 40 秒 —— 10-26 16:17 那次整轮零落库，最可能就死在
 * 这一步：只点一次火，失败即 return，crawl_logs 里一条记录都没有，事后看起来
 * 和「任务根本没被触发」完全一样。
 * 现在非 200/409 一律 15 秒后重试，最多 3 次。409 视为正常（目标域正在执行）。
 */
async function fire(domains) {
  const q = domains && domains.length ? '?domains=' + domains.join(',') : '';
  const label = domains ? domains.join(',') : '整轮';
  let lastErr = null;
  for (let i = 1; i <= IGNITE_TRIES; i++) {
    try {
      const r = await request('/api/crawl/daily-refresh' + q, cookieHeaders(), 'POST');
      if (r.status === 200 || r.status === 409) {
        console.log(`[fire] ${label} -> HTTP ${r.status} ${String(r.body).slice(0, 200)}（第 ${i} 次）`);
        return r;
      }
      lastErr = new Error('HTTP ' + r.status + ' ' + String(r.body).slice(0, 120));
    } catch (e) {
      lastErr = e;
    }
    console.log(`[fire] ${label} 第 ${i}/${IGNITE_TRIES} 次点火失败：${lastErr.message}`);
    if (i < IGNITE_TRIES) await sleep(IGNITE_GAP_MS);
  }
  throw lastErr || new Error('点火失败');
}

function cookieHeaders() {
  return { Cookie: 'session_token=' + TOKEN };
}

/** 轮询直到 targets 全部落库 / 超时 / 预算用尽。 */
async function waitFor(targets, budgetMs, sinceAt) {
  const until = Date.now() + budgetMs;
  let last = { got: new Set(), detail: {}, failedToday: {} };
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
    // **失败即终局**：域已写出 failed 行就说明这一轮跑完了（只是失败），
    // 继续等只会白耗到 800 秒，还挡着后面的补跑。
    // 只认「本轮开始之后」的失败行（sinceAt）：补跑时上一轮的 failed 行还在，
    // 否则一进补跑就会被旧失败行判成「已失败」而立刻退出。
    const isFreshFail = (t) =>
      last.failedToday && last.failedToday[t] && last.failedToday[t] >= sinceAt;
    if (missing.every(isFreshFail)) {
      console.log(
        `[poll ${elapsed()}s] 缺失域均已失败（${missing
          .map((t) => `${t}@${last.failedToday[t]}`)
          .join(',')}），提前进入补跑`
      );
      break;
    }
  }
  return last;
}

exports.main = async (event, context) => {
  if (!TOKEN || !KEY) {
    return { ok: false, msg: '缺少环境变量 EVENTDRIVE_SESSION_TOKEN / EVENTDRIVE_CLOUD_DB_KEY' };
  }
  const out = { ok: false, step: 'start' };

  // 1) 点火整轮（接口立即返回，抓取在沙箱后台线程跑）
  let igniteAt = bjtClock();
  try {
    const r = await fire(null);
    if (r.status !== 200 && r.status !== 409) {
      throw new Error('HTTP ' + r.status + ' ' + String(r.body).slice(0, 200));
    }
    igniteAt = bjtClock();
    out.ignite = r.body;
    out.step = 'ignited';
  } catch (e) {
    console.log('[1] 点火失败:', e.message);
    return { ok: false, step: 'ignite_failed', error: String(e.message).slice(0, 300) };
  }

  // 2) 自适应等待：三域齐了就提前收工，不再死等固定时长
  let state = await waitFor(NEED, DEADLINE_MS - (Date.now() - START), igniteAt);
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
        const retryAt = bjtClock();
        const r = await fire(retried);
        // 200 也可能是「这些域正在跑，本次跳过」（body.status = running），
        // 此时没必要再等 —— 只有真正 started 才值得等落库
        let body = {};
        try {
          body = JSON.parse(r.body);
        } catch (e) {}
        // 409 = 该域仍在跑（进程活着，只是慢），不该再点火
        if (r.status === 200 && body.status === 'started') {
          state = await waitFor(retried, left - 10 * 1000, retryAt);
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
    // 今天已写出 failed 行的域（新：2026-10-26 起失败才会留痕）
    failed_today: state.failedToday || {},
    hint,
  };
};
