/**
 * CloudBase 云函数：EventDrive 每日收盘抓取（北京时间 16:17）
 *
 * 与腾讯云 SCF 版的差异（三个）：
 * 1. 导出方式：CloudBase 用 exports.main，SCF 用 main_handler(event, context)
 * 2. 定时触发器 cron 是七段（首位为秒）：0 17 16 * * 1-5 *
 *    SCF 是六段：17 16 * * * *（无秒位）
 * 3. 环境变量在 CloudBase 控制台「函数配置 → 环境变量」配置
 *
 * 注意：函数运行时长上限通常 60~900 秒，需在控制台调大；
 * 本函数默认只等待约 700 秒（7 段 × 100 秒）。
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const TOKEN = process.env.EVENTDRIVE_SESSION_TOKEN || '';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';

const NEED = ['news', 'market', 'macro', 'stock_temp', 'hot_sector'];

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

exports.main = async (event, context) => {
  if (!TOKEN || !KEY) {
    return { ok: false, msg: '缺少环境变量 EVENTDRIVE_SESSION_TOKEN / EVENTDRIVE_CLOUD_DB_KEY' };
  }
  const cookie = { Cookie: 'session_token=' + TOKEN };
  const out = { ok: false, step: 'start' };

  // 1) 点火整轮（接口立即返回，抓取在沙箱后台跑）
  try {
    const r = await request('/api/crawl/daily-refresh', cookie, 'POST');
    console.log('[1] 点火响应:', r.body);
    if (r.status !== 200) throw new Error('HTTP ' + r.status);
    out.ignite = r.body;
    out.step = 'ignited';
  } catch (e) {
    console.log('[1] 点火失败:', e.message);
    return { ok: false, step: 'ignite_failed', error: String(e.message).slice(0, 300) };
  }

  // 2) 分段等待（每段 100 秒，避免单次过长被判定超时）
  let status = {};
  for (let i = 1; i <= 7; i++) {
    await sleep(100 * 1000);
    try {
      const r = await request('/api/crawl/daily-status', cookie);
      status = JSON.parse(r.body);
      console.log(`[2] 第${i * 100}秒:`, r.body);
    } catch (e) {
      console.log(`[2] 第${i * 100}秒查询失败:`, e.message);
    }
  }

  // 3) 校验逐域落库（crawl_time 存 UTC，比北京时间少 8 小时）
  let rows;
  try {
    const r = await request(
      '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status&order=crawl_time.desc&limit=30',
      { 'x-wb-webapp-access-key': KEY }
    );
    rows = JSON.parse(r.body);
  } catch (e) {
    console.log('[3] 拉 crawl_logs 失败:', e.message);
    return { ok: false, step: 'logs_failed', error: String(e.message).slice(0, 300) };
  }

  // CloudBase 运行时为 UTC，转北京时间（+8）后再判断「今天」
  const nowBJT = new Date(Date.now() + 8 * 3600 * 1000);
  const today = nowBJT.toISOString().slice(0, 10);

  const latest = {};
  for (const r of rows) {
    // '2026-10-19T11:37:43.210258' -> 视为 UTC，+8h 后取日期
    const t = new Date(r.crawl_time + 'Z');
    const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
    if (d === today && !latest[r.task]) latest[r.task] = t;
  }

  const okList = [], missing = [];
  for (const k of NEED) {
    if (latest[k]) {
      okList.push(k);
      const hh = new Date(latest[k].getTime() + 8 * 3600 * 1000);
      console.log(`[3]   OK   ${k.padEnd(11)} ${hh.toISOString().slice(11, 19)}`);
    } else {
      missing.push(k);
      console.log(`[3]   MISS ${k.padEnd(11)} -> 需手动补跑 ?domains=${k}`);
    }
  }

  return {
    ok: missing.length === 0,
    step: 'done',
    ok_domains: okList,
    missing,
    status,
    hint: missing.length === 0
      ? '全部落库'
      : '缺失域手动补跑: POST /api/crawl/daily-refresh?domains=' + missing.join(','),
  };
};