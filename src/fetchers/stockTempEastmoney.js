"use strict";
/** 股市温度 · 东方财富数据源抓取。
 *
 * 全部直连东财公开接口（无需 akshare），拉取失败抛异常，由 service 捕获后该指标记 error、
 * 保留上一次成功读数、绝不写兜底值。
 *
 * 指标：turnover_amt / turnover_rate / total_float_mcap（A 股全市场汇总一次拉取）、
 *       margin（两融）、etf_share、new_fund、industry_capital、structure_div。
 */

const { SOURCES } = require("./sources");
const { asJson, toFloat, parseNewfunddata } = require("./parsers");
const {
  num, toDateStr, percentileRank, dumpDetail, cleanSeries,
} = require("../services/stockTempIndicators");

const EM_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get";

/** 以 Asia/Shanghai 为准的今日日期（YYYY-MM-DD），避免服务器时区漂移。*/
function todayCST() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
}
function daysAgoCST(n) {
  const d = new Date(Date.now() + 8 * 3600 * 1000 - n * 86400 * 1000);
  return d.toISOString().slice(0, 10);
}

async function getJson(client, url, headers) {
  const r = await client.get(url, headers, { noCache: true });
  if (r.status < 200 || r.status >= 300) {
    throw new Error(`HTTP ${r.status}`);
  }
  const j = asJson(r.body);
  if (!j) throw new Error("返回非 JSON");
  return j;
}

// ------------------------------------------------------------ 两市成交额 / 流通市值 / 换手率
/** 东财 A 股全市场 clist，按页拉取直到取完。返回原始行数组。*/
async function fetchAllClist(client, fs, fields, maxPages = 12) {
  const rows = [];
  let total = 0;
  for (let pn = 1; pn <= maxPages; pn++) {
    const url =
      `https://push2.eastmoney.com/api/qt/clist/get?pn=${pn}&pz=5000&po=1&np=1` +
      `&fltt=2&invt=2&fid=f6&fs=${encodeURIComponent(fs)}&fields=${fields}`;
    const j = await getJson(client, url, SOURCES.em_quote);
    const data = (j && j.data) || {};
    total = Number(data.total) || 0;
    const diff = data.diff || [];
    if (!diff.length) break;
    for (const row of diff) rows.push(row);
    if (rows.length >= total) break;
  }
  return { rows, total };
}

const A_SHARE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23";

/** 一次拉取得到 两市成交额 / 全A换手率 / 两市流通市值 三项（亿元 / %）。*/
async function fetchMarketStats(client) {
  const { rows } = await fetchAllClist(client, A_SHARE_FS, "f6,f21");
  let turnover = 0; // 元
  let floatMcap = 0; // 元
  let n = 0;
  for (const r of rows) {
    const f6 = toFloat(r.f6);
    const f21 = toFloat(r.f21);
    if (f6 != null) { turnover += f6; n++; }
    if (f21 != null) floatMcap += f21;
  }
  if (!n || turnover <= 0) throw new Error("A 股行情汇总无成交额数据");
  const turnoverYi = Math.round((turnover / 1e8) * 100) / 100; // 亿元
  const floatMcapYi = floatMcap > 0 ? Math.round((floatMcap / 1e8) * 100) / 100 : null;
  const rate = floatMcapYi ? Math.round((turnover / floatMcap) * 100 * 1000) / 1000 : null;
  const asOf = todayCST();
  return {
    turnover_amt: {
      value: turnoverYi,
      as_of: asOf,
      source: "eastmoney",
      detail: {
        成交额: turnoverYi,
        较前一日: null,
        数据源: "东方财富 A 股全市场行情汇总（沪+深）",
      },
    },
    turnover_rate: {
      value: rate,
      as_of: asOf,
      source: "eastmoney",
      detail: {
        换手率: rate,
        数据源: "东方财富 A 股全市场行情汇总（成交额÷流通市值）",
      },
    },
    total_float_mcap: {
      value: floatMcapYi,
      as_of: asOf,
      source: "eastmoney",
      detail: {
        流通市值: floatMcapYi,
        数据源: "东方财富 A 股全市场行情汇总",
      },
    },
    _float_mcap_yi: floatMcapYi,
  };
}

// ------------------------------------------------------------ 两融
async function fetchMargin(client, floatMcapYi) {
  const url =
    `${EM_DATA}?reportName=RPTA_WEB_MARGIN_DAILYTRADE&columns=ALL&source=WEB&client=WEB` +
    `&pageNumber=1&pageSize=3&sortColumns=STATISTICS_DATE&sortTypes=-1`;
  const j = await getJson(client, url, SOURCES.em_data);
  const rows = ((j && j.result && j.result.data) || []);
  if (!rows.length) throw new Error("两融报表无数据");
  const r = rows[0];
  const balance = toFloat(r.MARGIN_BALANCE); // 亿元
  const finBalance = toFloat(r.FIN_BALANCE);
  const loanBalance = toFloat(r.LOAN_BALANCE);
  if (balance == null) throw new Error("两融余额字段缺失");
  const asOf = toDateStr(r.STATISTICS_DATE) || todayCST();
  const ratio = floatMcapYi ? Math.round((balance / floatMcapYi) * 100 * 100) / 100 : null;
  return {
    value: ratio,
    as_of: asOf,
    source: "eastmoney",
    detail: {
      "两融余额(万亿)": Math.round((balance / 10000) * 10000) / 10000,
      "融资余额(亿)": finBalance,
      "融券余额(亿)": loanBalance,
      "占流通市值%": ratio,
      "流通市值(万亿)": floatMcapYi ? Math.round((floatMcapYi / 10000) * 100) / 100 : null,
      数据源: "东方财富 融资融券账户信息（RPTA_WEB_MARGIN_DAILYTRADE）",
    },
  };
}

// ------------------------------------------------------------ 股票 ETF 份额
const BROAD_ETF_RE =
  /(沪深300|中证300|中证500|中证1000|中证2000|中证A500|中证A50|中证A100|上证50|上证180|上证380|深证100|深证300|双创50|科创板50|科创50|科创100|科创200|创业板50|创业板|北证50)ETF/;
const ETF_EXCLUDE = ["联接", "增强", "LOF", "货币"];

async function fetchEtfShare(client, prevValue) {
  const { rows } = await fetchAllClist(client, "b:MK0021", "f12,f14,f2,f21");
  const picked = [];
  for (const r of rows) {
    const name = String(r.f14 || "").replace(/\s/g, "");
    if (!name.includes("ETF") || ETF_EXCLUDE.some((k) => name.includes(k))) continue;
    if (!BROAD_ETF_RE.test(name)) continue;
    const f2 = toFloat(r.f2);
    const f21 = toFloat(r.f21);
    if (!f2 || !f21 || f21 <= 0) continue;
    picked.push({ code: r.f12, name, sharesYi: f21 / f2 / 1e8 }); // 亿份
  }
  if (!picked.length) throw new Error("未匹配到宽基股票ETF 样本");
  const total = picked.reduce((a, p) => a + p.sharesYi, 0);
  picked.sort((a, b) => b.sharesYi - a.sharesYi);
  const rate = prevValue ? Math.round(((total - prevValue) / prevValue) * 100 * 100) / 100 : null;
  return {
    value: Math.round(total * 10) / 10,
    as_of: todayCST(),
    source: "eastmoney",
    detail: {
      "合计份额(亿份)": Math.round(total * 10) / 10,
      样本只数: picked.length,
      "较上次快照变化%": rate,
      前三大: picked.slice(0, 3).map((p) => `${p.name.slice(0, 16)} ${p.sharesYi.toFixed(1)}亿份`).join(" | "),
      数据源: "东方财富 ETF 行情汇总（宽基口径每日快照，变化需逐日积累）",
    },
  };
}

// ------------------------------------------------------------ 新基金发行
const EQUITY_TYPES = ["股票", "混合", "指数型-股票"];
function isEquity(t) {
  const s = String(t || "");
  return EQUITY_TYPES.some((k) => s.includes(k));
}

async function fetchNewFund(client) {
  const url =
    "https://fund.eastmoney.com/data/FundNewIssue.aspx?t=xcln&sort=jzrgq,desc&y=&page=1,200&isbuy=1&v=0.5";
  const r = await client.get(url, SOURCES.em_fund, { noCache: true });
  if (r.status < 200 || r.status >= 300) throw new Error(`HTTP ${r.status}`);
  const d = parseNewfunddata(r.body);
  const datas = (d && d.datas) || [];
  if (!datas.length) throw new Error("新发基金接口无数据");

  const monthly = {};
  const perDay = [];
  for (const row of datas) {
    // 字段顺序: [代码, 简称, 公司, 公司代码, 基金类型, ?, 成立日期, 募集份额(亿), ...]
    const type = row[4];
    if (!isEquity(type)) continue;
    const dStr = toDateStr(row[6]);
    const amt = toFloat(row[7]);
    if (!dStr || amt == null || amt <= 0) continue;
    perDay.push([dStr, amt]);
    const m = dStr.slice(0, 7);
    monthly[m] = Math.round(((monthly[m] || 0) + amt) * 100) / 100;
  }
  if (!perDay.length) throw new Error("新发基金中未解析到权益类募集份额");
  perDay.sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const series = Object.keys(monthly).sort().map((k) => ({ as_of: k, value: monthly[k] }));

  const today = todayCST();
  const windows = {};
  for (const [label, nd] of [["近30日", 30], ["近90日", 90], ["近365日", 365]]) {
    const start = daysAgoCST(nd);
    windows[label] = Math.round(perDay.filter(([d]) => d >= start).reduce((a, [, v]) => a + v, 0) * 100) / 100;
  }
  const value = windows["近30日"];
  const monthlyAvg = series.length
    ? Math.round((series.reduce((a, p) => a + p.value, 0) / series.length) * 100) / 100
    : null;
  let pct = null;
  let pctWindow = null;
  if (series.length >= 12) {
    [pct, pctWindow] = percentileRank(value, series, "M");
  }
  return {
    value,
    as_of: daysAgoCST(1),
    source: "eastmoney",
    detail: {
      "近30日募集(亿)": windows["近30日"],
      "近90日募集(亿)": windows["近90日"],
      "近365日募集(亿)": windows["近365日"],
      "月度均值(亿)": monthlyAvg,
      分位: pct,
      分位窗口: pctWindow,
      样本月数: series.length,
      数据源: "东方财富 新基金发行（仅股票型 / 混合型 / 指数型-股票）",
    },
    series,
  };
}

// ------------------------------------------------------------ 产业资本净增持
async function fetchIndustryCapital(client) {
  const start = daysAgoCST(30);
  const end = todayCST();
  const filter = encodeURIComponent(`(NOTICE_DATE>='${start}')(NOTICE_DATE<='${end}')`);
  let net = 0;
  let shares = 0;
  let used = 0;
  const maxPages = 6;
  for (let page = 1; page <= maxPages; page++) {
    const url =
      `${EM_DATA}?reportName=RPT_SHARE_HOLDER_INCREASE&columns=ALL&filter=${filter}` +
      `&pageNumber=${page}&pageSize=500&sortColumns=NOTICE_DATE&sortTypes=-1&source=WEB&client=WEB`;
    const j = await getJson(client, url, SOURCES.em_data);
    const rows = ((j && j.result && j.result.data) || []);
    const pages = Number((j && j.result && j.result.pages) || 1);
    for (const r of rows) {
      let n = toFloat(r.CHANGE_NUM_SYMBOL);
      if (n == null) {
        n = toFloat(r.CHANGE_NUM);
        if (n != null && String(r.DIRECTION || "") === "减持") n = -Math.abs(n);
      }
      if (n == null) continue;
      const price = toFloat(r.REAL_PRICE) || toFloat(r.CLOSE_PRICE) || toFloat(r.TRADE_AVERAGE_PRICE);
      shares += n;
      if (price) net += n * 1e4 * price; // n 单位：万股 → 元
      used++;
    }
    if (!rows.length || page >= pages) break;
  }
  const netYi = Math.round((net / 1e8) * 100) / 100;
  return {
    value: netYi,
    as_of: end,
    source: "eastmoney",
    detail: {
      "近30日净增持(亿)": netYi,
      "净变动股份(万股)": Math.round(shares * 100) / 100,
      记录数: used,
      数据源: "东方财富 重要股东增减持（RPT_SHARE_HOLDER_INCREASE，近30日滚动）",
    },
  };
}

// ------------------------------------------------------------ 结构分化（科创50 vs 创业板指 价格分位差）
async function fetchKline(client, secid) {
  const url =
    `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=${secid}` +
    `&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61` +
    `&klt=101&fqt=1&beg=20200101&end=20500101&lmt=10000`;
  const j = await getJson(client, url, SOURCES.em_quote);
  const klines = ((j && j.data && j.data.klines) || []);
  const name = (j && j.data && j.data.name) || secid;
  const rows = [];
  for (const line of klines) {
    const parts = String(line).split(",");
    const asOf = parts[0];
    const close = toFloat(parts[2]); // f53=收盘
    if (asOf && close != null) rows.push({ as_of: asOf, value: close });
  }
  rows.sort((a, b) => (a.as_of < b.as_of ? -1 : 1));
  return { name, rows };
}

async function fetchStructureDiv(client) {
  const [kc, cyb] = await Promise.all([
    fetchKline(client, "1.000688"), // 科创50
    fetchKline(client, "0.399006"), // 创业板指
  ]);
  if (!kc.rows.length || !cyb.rows.length) throw new Error("指数 K 线无数据");
  const kcLast = kc.rows[kc.rows.length - 1];
  const cybLast = cyb.rows[cyb.rows.length - 1];
  const [kcPct, kcWindow] = percentileRank(kcLast.value, kc.rows, "D");
  const [cybPct, cybWindow] = percentileRank(cybLast.value, cyb.rows, "D");
  const diff = kcPct != null && cybPct != null ? Math.round((kcPct - cybPct) * 10) / 10 : null;
  return {
    value: diff,
    as_of: kcLast.as_of >= cybLast.as_of ? kcLast.as_of : cybLast.as_of,
    source: "eastmoney",
    detail: {
      科创50分位: kcPct,
      创业板分位: cybPct,
      分位差: diff,
      科创50收盘: kcLast.value,
      创业板收盘: cybLast.value,
      科创50窗口: kcWindow,
      创业板窗口: cybWindow,
      数据源: "东方财富 指数日K（科创50 / 创业板指 价格分位）",
    },
  };
}

module.exports = {
  todayCST,
  fetchMarketStats,
  fetchMargin,
  fetchEtfShare,
  fetchNewFund,
  fetchIndustryCapital,
  fetchStructureDiv,
  // 供 service 复用
  cleanSeries,
  dumpDetail,
};
