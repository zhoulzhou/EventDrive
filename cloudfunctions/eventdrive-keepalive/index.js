/**
 * CloudBase 云函数：EventDrive 沙箱保活（每 1 分钟一次）
 *
 * 为什么需要它
 * ------------
 * 应用托管在云沙箱里，实例空闲一段时间会被回收（缩容）。下一次有人访问时
 * 要重新冷启动：拉 Python 进程、读 index/ CSV、连云库……用户看到的就是
 * 「打开很慢」甚至 502/504。抓取任务更是直接受害 —— 进程被杀，跑到一半
 * 的域就没了（2026-10-19 丢过 stock_temp）。
 *
 * 本函数就是外部那只「按门铃的手」：定时打一次最轻的 /health，让实例始终
 * 处于有流量的状态，不给它进入空闲回收的机会。
 *
 * 三个必须遵守的细节（都是踩过的坑）
 * ----------------------------------
 * 1) **必须校验响应体，不能只看状态码。**
 *    网关有 5~10% 的概率返回 `200 + 纯文本 "Not Found"`（9 字节，
 *    Server: CloudStudio Gateway，2026-10-25 实测跨接口随机出现，连
 *    /health 都会中）。只看 statusCode 会把网关抖动当成「沙箱活着」，
 *    探针形同虚设。这里要求：200 且能 JSON.parse 且 body.status === 'ok'。
 * 2) **必须重试。** 上述假 404 是随机出现的，单次失败说明不了任何事。
 * 3) **只看 /health，绝不碰 /api/\*。** 保活一天 1440 次，必须足够轻：
 *    /health 不查库、不鉴权、不渲染模板，几十毫秒返回。打业务接口会白白
 *    消耗云库往返与配额。
 *
 * 重启可见化
 * ----------
 * 应用每次启动都会写一行 crawl_logs(task=instance_boot)，/health 也带
 * boot_at / uptime_s。本函数在 **uptime_s 明显小于心跳间隔** 时判定
 * 「这一轮里实例死过又起来」，记一条 status=warn 的日志（只在异常时写，
 * 正常心跳不写，避免一天 288 行噪音）。若平台是「非空闲原因」强制回收，
 * 保活挡不住 —— 那时的证据就是这些 warn 行 + instance_boot 行。
 *
 * ⚠️ 频率：**每 1 分钟**（见 config.json）。这不是拍脑袋 —— 2026-10-26 实测：
 *    5 分钟档位下，心跳打到时实例已经死了，每次心跳都在触发冷启动
 *    （13:51:43 起的实例，13:55:03 的心跳又拉起一个新实例），等于没保活。
 *    回收窗口在 2~3 分钟量级，只有 ≤1 分钟的心跳才续得上。
 *    CloudBase cron 是**七段**（首位为秒），北京时间。
 *    （注意：这里不能直接写出表达式 —— 其中的「星号 + 斜杠」会提前结束本段
 *      块注释，是 JS 里非常常见的自伤，改注释时别踩。）
 */

const https = require('https');

const BASE = 'https://eventdrive-finance.app.workbuddy.host';
const KEY = process.env.EVENTDRIVE_CLOUD_DB_KEY || '';

// 单次请求超时。/health 正常几十毫秒，给 15s 是留给冷启动（进程重新拉起要几秒）。
const TIMEOUT_MS = 15000;
// 重试次数与间隔：覆盖网关 5~10% 的随机假 404
// （间隔可用环境变量覆盖，只为本地桩件测试提速，线上不要设）
const MAX_TRIES = 3;
const RETRY_GAP_MS = parseInt(
  process.env.EVENTDRIVE_KEEPALIVE_RETRY_GAP_MS || '10000',
  10
);
// 心跳间隔（秒）与「疑似中途重启」的判定阈值（留 15s 给调度/网络抖动）
const HEARTBEAT_S = 60;
const RESTART_HINT_S = HEARTBEAT_S - 15;

/** 统一请求：返回 {status, body}；网络层错误直接 reject */
function request(path, opts) {
  const o = opts || {};
  const payload = o.body ? Buffer.from(o.body) : null;
  const headers = Object.assign(
    { 'Cache-Control': 'no-cache' },
    o.headers || {}
  );
  if (payload) {
    headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    headers['Content-Length'] = payload.length;
  }
  return new Promise((resolve, reject) => {
    const req = https.request(
      BASE + path,
      { method: o.method || 'GET', headers, timeout: TIMEOUT_MS },
      (res) => {
        let data = '';
        res.on('data', (c) => (data += c));
        res.on('end', () => resolve({ status: res.statusCode, body: data }));
      }
    );
    req.on('timeout', () => req.destroy(new Error('timeout ' + TIMEOUT_MS + 'ms')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** crawl_time 存 UTC（项目约定：时间列一律 naive UTC） */
function utcNow() {
  return new Date().toISOString().slice(0, 19).replace('T', ' ');
}

/**
 * 只在**保活失败**时写一行 crawl_logs（status=failed），正常心跳不写。
 * 应用挂掉时它自己写不了 instance_boot，这条是唯一的云端证据。
 * 写库失败不影响返回值 —— 探针的第一职责是把「活没活」告诉调用方。
 */
async function logEvent(status, message) {
  if (!KEY) return 'skip(no key)';
  try {
    const r = await request('/.cloud/database/rest/crawl_logs', {
      method: 'POST',
      headers: { 'x-wb-webapp-access-key': KEY },
      body: JSON.stringify([
        {
          task: 'keepalive',
          session: 'heartbeat',
          source: 'cloudfunction',
          item_count: 0,
          status: status,
          error_message: String(message).slice(0, 300),
          duration: 0,
          crawl_time: utcNow(),
        },
      ]),
    });
    return 'HTTP ' + r.status;
  } catch (e) {
    return 'error ' + e.message;
  }
}

exports.main = async () => {
  const t0 = Date.now();
  let lastErr = null;

  for (let i = 1; i <= MAX_TRIES; i++) {
    try {
      const r = await request('/health');
      let body = null;
      try {
        body = JSON.parse(r.body);
      } catch (e) {
        body = null; // 网关的 9 字节 "Not Found" 会走到这里
      }

      if (r.status === 200 && body && body.status === 'ok') {
        const uptime = typeof body.uptime_s === 'number' ? body.uptime_s : null;
        // 心跳间隔内死过又起来：uptime 会明显小于间隔
        const restarted = uptime !== null && uptime < RESTART_HINT_S;
        const out = {
          ok: true,
          attempts: i,
          latency_ms: Date.now() - t0,
          boot_at: body.boot_at || null,
          uptime_s: uptime,
          suspicious_restart: restarted,
        };
        if (restarted) {
          // 只告警、不写库：应用启动时已经写了一条 crawl_logs(instance_boot)，
          // 那才是权威证据（精确到启动时刻）。这里只是「看起来像重启」的推断，
          // 而且发布本身也会造成 uptime 很小 —— 再落一条 warn 只会污染日志。
          console.warn(
            `[keepalive] 实例 uptime 仅 ${uptime}s（< ${RESTART_HINT_S}s），` +
              `说明在本轮心跳间隔内启动过：发布重启或被平台回收。boot_at=${body.boot_at}`
          );
        }
        // 正常心跳**不打印**：一分钟一次、一天 1440 次，全打会把日志刷满。
        // 要确认心跳在跑，看 crawl_logs 里 instance_boot 是否停止增长即可。
        return out;
      }

      lastErr = new Error(
        `HTTP ${r.status} body=${String(r.body).replace(/\s+/g, ' ').slice(0, 80)}`
      );
    } catch (e) {
      lastErr = e;
    }

    console.warn(`[keepalive] 第 ${i}/${MAX_TRIES} 次失败: ${lastErr.message}`);
    if (i < MAX_TRIES) await sleep(RETRY_GAP_MS);
  }

  const msg = `保活失败（${MAX_TRIES} 次）：${lastErr ? lastErr.message : 'unknown'}`;
  console.error('[keepalive] ' + msg);
  const logged = await logEvent('failed', msg);
  return {
    ok: false,
    attempts: MAX_TRIES,
    latency_ms: Date.now() - t0,
    error: lastErr ? String(lastErr.message).slice(0, 300) : 'unknown',
    logged,
    hint:
      '沙箱可能真的挂了，或被平台强制回收：查 CloudBase 应用日志；' +
      '若同时段 crawl_logs 里有 instance_boot 行，说明实例已重建。',
  };
};
