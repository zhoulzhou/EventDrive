/**
 * CloudBase 云函数：EventDrive 早间新闻抓取（北京时间 9:15）
 *
 * 与收盘函数 eventdrive-refresh 的区别：
 * 1. **只抓 news + market 两个域**（用 `?domains=news,market`），
 *    不跑宏观/热点/股市热度这些「A 股收盘后才有意义」的域
 * 2. 等待时间短得多（新闻 10~30 秒 + 行情 20~40 秒，故只等 3 段 × 60 秒）
 *
 * ⚠️ 为什么 market 也放在早上（2026-10-31 加）：
 *    美股收盘是北京时间凌晨 4~5 点，收盘轮跑在 21:34（美东 09:34，刚开盘），
 *    只能拿到**上一个**交易日；周六周日没有收盘轮，于是整个周末页面都停在周四。
 *    早上 9:15（美东前一天 21:15，早已收盘）跑一次 market，周二~周六都能拿到
 *    刚刚结束的那个交易日，周末不再掉一天。
 *
 * 为什么要单独一个函数（且新闻已独立于收盘轮）：收盘函数跑三个域要 3~7 分钟，函数必须等那么久；
 * 早上只补新闻的话，等那么久纯属浪费配额与超时风险。
 *
 * ⚠️ 2026-10-31 起 market **只在这一轮抓**（晚上 21:34 那轮已不再抓行情），
 *    所以校验从「/daily-status 说不跑了」改成「crawl_logs 里今天真有 ok/empty 行」，
 *    并在缺域时**自动按域补跑一次**（再等 2 分钟）。否则行情失败一次，页面就整天停在前一天。
 *
 * ⚠️ CloudBase cron 是**七段**（首位为秒），北京时间 = UTC+8：
 *    `0 15 9 * * * *` = 每天 9:15:00（每天都跑，不限工作日）
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const TOKEN = process.env.EVENTDRIVE_SESSION_TOKEN || '';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';

const NEED = ['news', 'market'];
const DOMAINS = NEED.join(',');

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

const POLL_MS = 20 * 1000;
const START = Date.now();
const elapsed = () => Math.round((Date.now() - START) / 1000);

/** 读 crawl_logs，返回「今天（北京时间）已落库 / 失败」的域集合。 */
async function readLanded() {
  const landed = {};
  const errors = {};
  if (!KEY) return { landed, errors };
  const r = await request(
    '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status&order=crawl_time.desc&limit=30',
    { 'x-wb-webapp-access-key': KEY }
  );
  if (r.status !== 200) throw new Error('crawl_logs HTTP ' + r.status);
  const rows = JSON.parse(r.body);
  const nowBJT = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  // 2026-10-26：失败也会写 status=failed，不能把 failed 当成「已落库」，
  // 否则源挂了也会报成功（记到 errors 里照实说明）。
  // 顺序是 crawl_time desc：今天成功过就算跑过了（新的失败不覆盖旧的成功）。
  for (const row of rows) {
    const t = new Date(row.crawl_time + 'Z');
    const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
    if (d !== nowBJT || !NEED.includes(row.task)) continue;
    if (row.status === 'ok' || row.status === 'empty') {
      if (!landed[row.task]) landed[row.task] = { item_count: row.item_count, status: row.status };
      continue;
    }
    if (!errors[row.task]) errors[row.task] = { status: row.status };
  }
  return { landed, errors };
}

/** 轮询直到 targets 全部落库或预算用尽。 */
async function waitLanded(targets, budgetMs) {
  const until = Date.now() + budgetMs;
  let last = { landed: {}, errors: {} };
  while (Date.now() < until) {
    await sleep(POLL_MS);
    try {
      last = await readLanded();
    } catch (e) {
      console.log(`[poll ${elapsed()}s] 拉 crawl_logs 失败:`, e.message);
      continue;
    }
    const missing = targets.filter((d) => !last.landed[d]);
    console.log(
      `[poll ${elapsed()}s] 已落库 ${targets.length - missing.length}/${targets.length}` +
        (missing.length ? ' 缺: ' + missing.join(',') : ' 齐了')
    );
    if (!missing.length) break;
  }
  return last;
}

exports.main = async (event, context) => {
  if (!TOKEN) {
    return { ok: false, msg: '缺少环境变量 EVENTDRIVE_SESSION_TOKEN' };
  }
  const cookie = { Cookie: 'session_token=' + TOKEN };
  const out = { ok: false, step: 'start' };

  // 1) 点火（只抓 news）。接口立即返回，抓取在沙箱后台跑。
  //    2026-10-26：加 3 次重试（间隔 15s）—— 沙箱冷启动时首个请求常超过
  //    本函数 40s 的单请求超时，一次失败就 return 会让整轮彻底落空。
  try {
    let lastErr = null;
    let r = null;
    for (let i = 1; i <= 3; i++) {
      try {
        r = await request('/api/crawl/daily-refresh?domains=' + DOMAINS, cookie, 'POST');
        if (r.status === 200) break;
        lastErr = new Error('HTTP ' + r.status + ' ' + String(r.body).slice(0, 120));
      } catch (e) {
        lastErr = e;
      }
      console.log(`[1] 点火第 ${i}/3 次失败:`, lastErr.message);
      if (i < 3) await sleep(15 * 1000);
      r = null;
    }
    if (!r) throw lastErr || new Error('点火失败');
    console.log('[1] 点火响应:', r.body);
    out.ignite = r.body;
    out.step = 'ignited';
  } catch (e) {
    console.log('[1] 点火失败:', e.message);
    return { ok: false, step: 'ignite_failed', error: String(e.message).slice(0, 300) };
  }

  // 2) 轮询 crawl_logs 直到 news/market 都落库（最多 3 段 × 60 秒 = 3 分钟）
  //    2026-10-31 改：原来靠 /daily-status 的 running 判断「跑完了」，但那只说明
  //    后台线程结束，不说明**落库成功**；行情现在是唯一的自动窗口（晚上那轮已不抓），
  //    必须确认真落了库，否则页面会整天停在前一天。
  let state = await waitLanded(NEED, 3 * 60 * 1000);
  let missing = NEED.filter((d) => !state.landed[d]);
  console.log('[2] 首轮结束，缺:', missing.join(',') || '无');

  // 3) 有缺域 -> 按域补跑一次再等 2 分钟（整轮预算控制在 5 分钟内，Timeout 900s 够用）
  let retried = [];
  if (missing.length) {
    retried = missing.slice();
    try {
      const r = await request('/api/crawl/daily-refresh?domains=' + retried.join(','), cookie, 'POST');
      let body = {};
      try {
        body = JSON.parse(r.body);
      } catch (e) {}
      console.log('[3] 补跑', retried.join(','), '-> HTTP', r.status, JSON.stringify(body).slice(0, 160));
      // 200 也可能是「这些域正在跑，本次跳过」（status=running），那就只等不重新点火
      if (r.status === 200 || r.status === 409) {
        state = await waitLanded(retried, 2 * 60 * 1000);
      }
    } catch (e) {
      console.log('[3] 补跑点火失败:', e.message);
    }
    missing = NEED.filter((d) => !state.landed[d]);
  }

  return {
    ok: missing.length === 0,
    step: 'done',
    domains: NEED,
    landed: state.landed,
    errors: state.errors,
    retried,
    hint: missing.length
      ? '未查到今日 ' + missing.join('/') + ' 落库记录，可手动补跑: '
        + 'POST /api/crawl/daily-refresh?domains=' + missing.join(',')
      : '均已落库',
  };
};
