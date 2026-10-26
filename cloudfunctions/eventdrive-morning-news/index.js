/**
 * CloudBase 云函数：EventDrive 早间新闻抓取（北京时间 9:15）
 *
 * 与收盘函数 eventdrive-refresh 的区别：
 * 1. **只抓 news 一个域**（用 `?domains=news`），不跑其余域（市场/宏观/热点/股市热度）
 * 2. **不校验逐域落库** —— 只抓一个域，直接轮询到结束即可
 * 3. 等待时间短得多（新闻抓取约 10~30 秒，故只等 3 段 × 60 秒）
 *
 * 为什么要单独一个函数（且新闻已独立于收盘轮）：收盘函数跑三个域要 3~7 分钟，函数必须等那么久；
 * 早上只补新闻的话，等那么久纯属浪费配额与超时风险。
 *
 * ⚠️ CloudBase cron 是**七段**（首位为秒），北京时间 = UTC+8：
 *    `0 15 9 * * * *` = 每天 9:15:00（每天都跑，不限工作日）
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const TOKEN = process.env.EVENTDRIVE_SESSION_TOKEN || '';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';

const NEED = 'news';

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
        r = await request('/api/crawl/daily-refresh?domains=' + NEED, cookie, 'POST');
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

  // 2) 轮询直到不再运行（最多 3 段 × 60 秒 = 3 分钟）
  let status = {};
  for (let i = 1; i <= 3; i++) {
    await sleep(60 * 1000);
    try {
      const r = await request('/api/crawl/daily-status', cookie);
      status = JSON.parse(r.body);
      console.log(`[2] 第${i * 60}秒 running=${status.running}`);
      if (status.running === false && i >= 1) break;
    } catch (e) {
      console.log(`[2] 第${i * 60}秒查询失败:`, e.message);
    }
  }

  // 3) 可选：校验 crawl_logs 里今天有没有 news 记录（crawl_time 存 UTC，需 +8h）
  let landed = null;
  if (KEY) {
    try {
      const r = await request(
        '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status&order=crawl_time.desc&limit=20',
        { 'x-wb-webapp-access-key': KEY }
      );
      const rows = JSON.parse(r.body);
      const nowBJT = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
      // 2026-10-26：失败也会写 status=failed，不能把 failed 当成「已落库」，
      // 否则源挂了也会报成功（记到 lastError 里照实说明）。
      let lastError = null;
      for (const row of rows) {
        const t = new Date(row.crawl_time + 'Z');
        const d = new Date(t.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
        if (d !== nowBJT || row.task !== NEED) continue;
        if (row.status === 'ok' || row.status === 'empty') {
          landed = { item_count: row.item_count, status: row.status };
          break;
        }
        if (!lastError) lastError = { status: row.status };
      }
      if (lastError) console.log('[3] 今日 news 无成功记录，最新状态:', JSON.stringify(lastError));
      console.log('[3] 今日 news 落库:', JSON.stringify(landed));
    } catch (e) {
      console.log('[3] 拉 crawl_logs 失败:', e.message);
    }
  }

  return {
    ok: landed !== null,
    step: 'done',
    domain: NEED,
    landed,
    status,
    hint: landed === null
      ? '未查到今日 news 落库记录，可手动补跑: POST /api/crawl/daily-refresh?domains=news'
      : 'news 已落库',
  };
};