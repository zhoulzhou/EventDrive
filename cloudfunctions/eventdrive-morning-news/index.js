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

  // 3) 可选：校验 crawl_logs 里今天有没有 news / market 记录（crawl_time 存 UTC，需 +8h）
  const landed = {};
  if (KEY) {
    try {
      const r = await request(
        '/.cloud/database/rest/crawl_logs?select=crawl_time,task,item_count,status&order=crawl_time.desc&limit=30',
        { 'x-wb-webapp-access-key': KEY }
      );
      const rows = JSON.parse(r.body);
      const nowBJT = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
      // 2026-10-26：失败也会写 status=failed，不能把 failed 当成「已落库」，
      // 否则源挂了也会报成功（记到 errors 里照实说明）。
      const errors = {};
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
      console.log('[3] 今日落库:', JSON.stringify(landed), '失败:', JSON.stringify(errors));
    } catch (e) {
      console.log('[3] 拉 crawl_logs 失败:', e.message);
    }
  }

  const missing = NEED.filter((d) => !landed[d]);
  return {
    ok: missing.length === 0,
    step: 'done',
    domains: NEED,
    landed,
    status,
    hint: missing.length
      ? '未查到今日 ' + missing.join('/') + ' 落库记录，可手动补跑: '
        + 'POST /api/crawl/daily-refresh?domains=' + missing.join(',')
      : '均已落库',
  };
};
