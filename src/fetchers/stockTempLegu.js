"use strict";
/** 股市温度 · 乐咕乐股（legulegu）数据源抓取。
 *
 * legulegu 的历史 API 需当日 token（401），这里只取页面 HTML 中服务端渲染的最新读数：
 *   - 沪深300 PE/PB：index-basic 页面的 <meta name="description">
 *   - 破净率：below-net-asset-statistics 页面的 metric-label/metric-value
 *   - 市场宽度：market-activity 页面的 <meta name="description">
 * 取不到完整历史 → 分位留空，由本页逐日积累后补算（见 service._fillPercentiles）。
 * 拉取失败抛异常，由 service 捕获后该指标记 error、保留上一次成功读数。
 */

const { SOURCES } = require("./sources");
const { num, toDateStr } = require("../services/stockTempIndicators");

const LEGU_HDR = {
  ...SOURCES.sina,
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
};

async function getHtml(client, url) {
  const r = await client.get(url, LEGU_HDR, { noCache: true });
  if (r.status < 200 || r.status >= 300) throw new Error(`HTTP ${r.status}`);
  return String(r.body || "");
}

function metaDescription(html) {
  const m = html.match(/name="description"\s+content="([^"]*)"/);
  return m ? m[1] : "";
}

// ------------------------------------------------------------ 沪深300 PE / PB
async function fetchPePb(client) {
  const html = await getHtml(client, "https://legulegu.com/stockdata/index-basic?indexCode=000300.SH");
  const desc = metaDescription(html);
  // 形如：2026-11-27，沪深300的最新加权平均市盈率：12.85，加权平均市净率：1.34，...
  const dateM = desc.match(/(\d{4}-\d{2}-\d{2})/);
  const peM = desc.match(/加权平均市盈率[：:]\s*([\d.]+)/);
  const pbM = desc.match(/加权平均市净率[：:]\s*([\d.]+)/);
  const asOf = dateM ? dateM[1] : null;
  const pe = peM ? num(peM[1]) : null;
  const pb = pbM ? num(pbM[1]) : null;
  if (pe == null) throw new Error("乐咕 PE 读数解析失败");
  if (!asOf) throw new Error("乐咕 PE 页面无日期");
  const base = { as_of: asOf, source: "legulegu" };
  return {
    hs300_pe: {
      ...base,
      value: pe,
      detail: {
        PE: pe,
        PB: pb,
        数据源: "乐咕乐股 index-basic（沪深300 加权平均市盈率，最新读数）",
      },
    },
    hs300_pb: {
      ...base,
      value: pb,
      detail: {
        PE: pe,
        PB: pb,
        数据源: "乐咕乐股 index-basic（沪深300 加权平均市净率，最新读数）",
      },
    },
  };
}

// ------------------------------------------------------------ 破净率
function metricMap(html) {
  const labels = [...html.matchAll(/<span class="metric-label">([^<]*)<\/span>/g)].map((m) =>
    m[1].replace(/\s/g, "")
  );
  const values = [...html.matchAll(/<span class="metric-value">([^<]*)<\/span>/g)].map((m) =>
    m[1].replace(/\s/g, "")
  );
  const map = {};
  for (let i = 0; i < Math.min(labels.length, values.length); i++) {
    map[labels[i]] = values[i];
  }
  return map;
}

async function fetchBelowNa(client) {
  const html = await getHtml(client, "https://legulegu.com/stockdata/below-net-asset-statistics");
  const map = metricMap(html);
  const ratioRaw = map["破净股比例"];
  const countRaw = map["破净股数量"];
  const desc = metaDescription(html);
  const dateM = desc.match(/(\d{4}-\d{2}-\d{2})/);
  const ratio = ratioRaw != null ? num(String(ratioRaw).replace("%", "")) : null;
  const count = countRaw != null ? num(String(countRaw).replace(/[^\d.]/g, "")) : null;
  if (ratio == null) throw new Error("乐咕破净率解析失败");
  const asOf = dateM ? dateM[1] : null;
  const total = ratio > 0 && count != null ? Math.round((count / ratio) * 100) : null;
  return {
    value: Math.round(ratio * 100) / 100,
    as_of: asOf,
    source: "legulegu",
    detail: {
      破净率: Math.round(ratio * 100) / 100,
      破净家数: count,
      公司总数: total,
      数据源: "乐咕乐股 破净股统计（全部A股，最新读数）",
    },
  };
}

// ------------------------------------------------------------ 市场宽度
async function fetchBreadth(client) {
  const html = await getHtml(client, "https://legulegu.com/stockdata/market-activity");
  const desc = metaDescription(html);
  // 形如：2026-11-27，A股赚钱效益分布，其中435家涨停，1家跌停，5025家上涨，181家下跌，648家上涨0%~3%...
  const dateM = desc.match(/(\d{4}-\d{2}-\d{2})/);
  const up = num((desc.match(/(\d+)\s*家上涨/) || [])[1]);
  const down = num((desc.match(/(\d+)\s*家下跌/) || [])[1]);
  const limitUp = num((desc.match(/(\d+)\s*家涨停/) || [])[1]);
  const limitDown = num((desc.match(/(\d+)\s*家跌停/) || [])[1]);
  const flat = num((desc.match(/(\d+)\s*家平盘/) || [])[1]);
  if (up == null || down == null || up + down <= 0) throw new Error("乐咕市场宽度涨跌家数解析失败");
  const rate = Math.round((up / (up + down)) * 1000) / 10;
  return {
    value: rate,
    as_of: dateM ? dateM[1] : null,
    source: "legulegu",
    detail: {
      上涨家数: up,
      下跌家数: down,
      涨停: limitUp || 0,
      跌停: limitDown || 0,
      平盘: flat || 0,
      数据源: "乐咕乐股 赚钱效应分布（最新读数）",
    },
  };
}

module.exports = { fetchPePb, fetchBelowNa, fetchBreadth };
