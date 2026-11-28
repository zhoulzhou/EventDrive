"use strict";
/**
 * 宏观指标 · 中国人民银行官网源（尽力而为，失败不抛、不入库、不兜底）。
 * 移植自 app/utils/pbc_data.py：正则解析 HTML，站点结构变化即自然缺失。
 *   - 逆回购操作利率（《公开市场业务交易公告》）
 *   - 社融存量同比（《金融统计数据报告》）
 *   - 企业/住户中长贷同比多增 + 财政存款当月增量（分部门存贷款）
 */
const client = require("./httpClient");
const { SOURCES } = require("./sources");

const SOURCE = "pbc";
const BASE_CANDIDATES = ["https://www.pbc.gov.cn", "http://www.pbc.gov.cn"];
const COL_OMO = "/zhengcehuobisi/125207/125213/125431/125475/index.html";
const COL_FIN_STAT = "/diaochatongjisi/116219/116225/index.html";

let workingBase = null;

async function pbcGet(pathOrUrl) {
  const roots = workingBase ? [workingBase] : BASE_CANDIDATES;
  let lastErr = null;
  for (const root of roots) {
    const full = pathOrUrl.startsWith("http") ? pathOrUrl : root + pathOrUrl;
    try {
      const r = await client.get(full, SOURCES.pbc);
      if (r.status >= 200 && r.status < 300) {
        if (!workingBase) workingBase = root;
        return r.body;
      }
      lastErr = new Error(`HTTP ${r.status}`);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("无法访问央行页面");
}

function stripHtml(html) {
  let t = String(html || "")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ");
  for (const [e, c] of [["&nbsp;", " "], ["&amp;", "&"], ["&ldquo;", "“"], ["&rdquo;", "”"], ["&mdash;", "—"], ["&middot;", "·"]]) {
    t = t.split(e).join(c);
  }
  return t.replace(/\s+/g, " ").trim();
}

async function pageText(url) { return stripHtml(await pbcGet(url)); }

function links(html) {
  const out = [];
  const re = /<a[^>]+href="([^"]+)"[^>]*>([^<]{2,160})<\/a>/g;
  let m;
  while ((m = re.exec(html)) != null) out.push([m[2].replace(/\s+/g, ""), m[1]]);
  return out;
}

function recentLinks(html, keyword, limit = 12) {
  const hits = [];
  for (const [title, href] of links(html)) {
    if (title.includes(keyword)) { hits.push([title, href]); if (hits.length >= limit) break; }
  }
  return hits;
}

function cnYmToIso(text) {
  const m = String(text || "").match(/(\d{4})\s*年\s*(\d{1,2})\s*月/);
  return m ? `${m[1]}-${String(m[2]).padStart(2, "0")}` : null;
}

function absUrl(href) {
  if (!href) return "";
  if (href.startsWith("http")) return href;
  return (workingBase || BASE_CANDIDATES[0]) + (href.startsWith("/") ? href : "/" + href);
}

// ---------------- 逆回购操作利率
const REPO_TERMS = ["隔夜", "7天", "14天", "28天"];
function parseRepoRow(text) {
  const flat = String(text || "").replace(/\s+/g, "").replace(/．/g, ".").replace(/％/g, "%");
  if (!flat.includes("逆回购")) return null;
  for (const term of REPO_TERMS) {
    const re = new RegExp(`${term}(?:期)?[^0-9％]{0,16}?(\\d+\\.\\d+)％`);
    const m = flat.match(re);
    if (m) return [term, parseFloat(m[1])];
  }
  return null;
}
function announceDate(text) {
  const m = String(text || "").match(/(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
}

async function fetchReverseRepoRate() {
  const html = await pbcGet(COL_OMO);
  const cands = recentLinks(html, "公开市场业务交易公告", 12);
  if (!cands.length) throw new Error("《公开市场业务交易公告》栏目未解析到任何条目");
  let lastErr = null;
  for (const [title, href] of cands) {
    let text;
    try { text = await pageText(href); } catch (e) { lastErr = e; continue; }
    const parsed = parseRepoRow(text);
    if (!parsed) continue;
    const [term, rate] = parsed;
    return {
      key: "reverse_repo_rate", value: rate, as_of: announceDate(text) || "", source: SOURCE,
      detail: { 公告: title, 期限: term, 来源: "中国人民银行公开市场业务交易公告", 链接: absUrl(href) },
    };
  }
  throw new Error(`最近公告中未找到逆回购操作利率（${lastErr ? lastErr.message : "均无可解析的操作行"}）`);
}

// ---------------- 社融存量同比
const TSF_RE = /社会融资规模存量为\s*([\d.]+)\s*万亿元[，,]?\s*同比增长\s*([\d.]+)\s*%/;
function reportPeriod(title) {
  const special = [
    [/(\d{4})\s*年\s*一季度/, 3], [/(\d{4})\s*年\s*上半年/, 6],
    [/(\d{4})\s*年\s*前三季度/, 9], [/(\d{4})\s*年\s*年度/, 12],
  ];
  for (const [re, mo] of special) {
    const m = title.match(re);
    if (m) return `${m[1]}-${String(mo).padStart(2, "0")}`;
  }
  return cnYmToIso(title);
}

async function fetchTsfStockYoy() {
  const html = await pbcGet(COL_FIN_STAT);
  const cands = recentLinks(html, "金融统计数据报告", 8);
  if (!cands.length) throw new Error("《金融统计数据报告》栏目未解析到任何条目");
  let lastErr = null;
  for (const [title, href] of cands) {
    let text;
    try { text = await pageText(href); } catch (e) { lastErr = e; continue; }
    const m = text.match(TSF_RE);
    if (!m) continue;
    return {
      key: "tsf_yoy", value: parseFloat(m[2]),
      as_of: reportPeriod(title) || cnYmToIso(text) || "", source: SOURCE,
      detail: { 报告: title, 存量: `${m[1]} 万亿元`, 存量万亿: parseFloat(m[1]), 来源: "中国人民银行《金融统计数据报告》" },
    };
  }
  throw new Error(`未能解析社融存量同比（${lastErr ? lastErr.message : "报告正文无匹配字段"}）`);
}

// ---------------- 分部门存贷款（企业/住户中长贷同比多增 + 财政存款当月增量）
const AMOUNT_RE = /([\d.]+)\s*(万亿元|亿元)/;
const DEPOSIT_SPLIT = {
  "住户存款": "住户存款", "住户贷款": "住户贷款",
  "非银行业金融机构存款": "非银存款", "非金融企业存款": "企业存款",
};
function amountToWanYi(num, unit) {
  const v = parseFloat(num);
  return unit === "亿元" ? Math.round((v / 10000) * 10000) / 10000 : Math.round(v * 10000) / 10000;
}
function parseMlLoan(flat, anchor, span = 180) {
  const idx = flat.indexOf(anchor);
  if (idx < 0) return null;
  const seg = flat.slice(idx, idx + span);
  const m = seg.match(/中长期贷款\s*(增加|减少|下降|上升)\s*([\d.]+)\s*(万亿元|亿元)/);
  if (!m) return null;
  const v = amountToWanYi(m[2], m[3]);
  return (m[1] === "减少" || m[1] === "下降") ? -v : v;
}
function parseFiscalDeposit(flat) {
  const m = flat.match(/财政性存款\s*(增加|减少|下降|上升)\s*([\d.]+)\s*(万亿元|亿元)/);
  if (!m) return null;
  const v = amountToWanYi(m[2], m[3]);
  return (m[1] === "减少" || m[1] === "下降") ? -v : v;
}
function parseSplitAmount(flat, name) {
  const re = new RegExp(`${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*(增加|减少|下降|上升)\\s*${AMOUNT_RE.source}`);
  const m = flat.match(re);
  if (!m) return null;
  let v = parseFloat(m[2]);
  if (m[3] === "亿元") v = v / 10000;
  return Math.round((m[1] === "减少" || m[1] === "下降" ? -v : v) * 10000) / 10000;
}
const TOTAL_DEPOSIT_RE = new RegExp(`(前[一二三四五六七八九十]+个月|上半年|前三季度|前?一季度|\\d{1,2}月份)人民币存款增加${AMOUNT_RE.source}`);
const TOTAL_DEPOSIT_LOOSE_RE = new RegExp(`人民币存款增加${AMOUNT_RE.source}`);
const WINDOW_RE = /(前[一二三四五六七八九十]+个月|上半年|前三季度|前?一季度|\d{1,2}月份)/;
function wanYi(v, unit) { return unit === "亿元" ? Math.round((v / 10000) * 10000) / 10000 : Math.round(v * 10000) / 10000; }

function parseSplitReport(title, flat) {
  if (!flat.includes("住户存款")) return null;
  const parsed = {};
  for (const [needle, label] of Object.entries(DEPOSIT_SPLIT)) parsed[label] = parseSplitAmount(flat, needle);
  if (parsed["住户贷款"] == null && parsed["住户存款"] == null) return null;
  let window = null, total = null;
  const m = flat.match(TOTAL_DEPOSIT_RE);
  if (m) { window = m[1]; total = wanYi(parseFloat(m[2]), m[3]); }
  if (total == null) {
    const m2 = flat.match(TOTAL_DEPOSIT_LOOSE_RE);
    if (m2) total = wanYi(parseFloat(m2[1]), m2[2]);
  }
  if (window == null) { const w = flat.match(WINDOW_RE); window = w ? w[1] : "累计"; }
  return {
    报告: title, as_of: reportPeriod(title) || cnYmToIso(flat) || "", 期间: window,
    人民币存款增加: total, 住户存款: parsed["住户存款"], 住户贷款: parsed["住户贷款"],
    非银存款: parsed["非银存款"], 企业存款: parsed["企业存款"],
    住户中长期贷款: parseMlLoan(flat, "住户贷款"), 企业中长期贷款: parseMlLoan(flat, "业单位贷款"),
    财政性存款: parseFiscalDeposit(flat),
  };
}
function monthBefore(period) {
  const m = String(period || "").match(/^(\d{4})-(\d{2})$/);
  if (!m) return null;
  const yr = parseInt(m[1], 10), mo = parseInt(m[2], 10);
  if (mo === 1) return null;
  return `${yr}-${String(mo - 1).padStart(2, "0")}`;
}
function sameMonthLastYear(period) {
  const m = String(period || "").match(/^(\d{4})-(\d{2})$/);
  return m ? `${parseInt(m[1], 10) - 1}-${m[2]}` : null;
}

async function fetchCreditBreakdown(periods = 16) {
  const html = await pbcGet(COL_FIN_STAT);
  const cands = recentLinks(html, "金融统计数据报告", Math.max(periods + 6, 12));
  if (!cands.length) throw new Error("《金融统计数据报告》栏目未解析到任何条目");
  const byPeriod = {};
  let lastErr = null;
  let got = 0;
  for (const [title, href] of cands) {
    if (got >= periods) break;
    let flat;
    try { flat = String(await pageText(href)).replace(/\s+/g, ""); }
    catch (e) { lastErr = e; continue; }
    const rec = parseSplitReport(title, flat);
    if (rec && rec.as_of) { byPeriod[rec.as_of] = rec; got++; }
  }
  const keys = Object.keys(byPeriod);
  if (!keys.length) throw new Error(`未能解析中长期贷款/财政存款（${lastErr ? lastErr.message : "报告正文无匹配字段"}）`);
  const latestPeriod = keys.sort().reverse()[0];
  const latest = byPeriod[latestPeriod];

  const flow = (period, field) => {
    if (!period) return null;
    const rec = byPeriod[period];
    if (!rec || rec[field] == null) return null;
    const cum = parseFloat(rec[field]);
    const prevPeriod = monthBefore(period);
    if (prevPeriod == null) return Math.round(cum * 10000);
    const prev = byPeriod[prevPeriod];
    if (!prev || prev[field] == null) return null;
    return Math.round((cum - parseFloat(prev[field])) * 10000);
  };
  const yoyDiff = (field) => {
    const cur = flow(latestPeriod, field);
    const last = flow(sameMonthLastYear(latestPeriod), field);
    return (cur == null || last == null) ? null : Math.round(cur - last);
  };
  const corpNow = flow(latestPeriod, "企业中长期贷款");
  const corpLast = flow(sameMonthLastYear(latestPeriod), "企业中长期贷款");
  const houseNow = flow(latestPeriod, "住户中长期贷款");
  const houseLast = flow(sameMonthLastYear(latestPeriod), "住户中长期贷款");
  const fiscalNow = flow(latestPeriod, "财政性存款");
  const fmt = (v) => (v == null ? "—" : `${v} 亿`);

  return {
    as_of: latestPeriod,
    corp_ml_yoy: yoyDiff("企业中长期贷款"),
    household_ml_yoy: yoyDiff("住户中长期贷款"),
    fiscal_deposit_mom: fiscalNow,
    detail: {
      报告: latest["报告"], 数据期: latestPeriod,
      企业中长期贷款: `当月 ${fmt(corpNow)} · 去年同期 ${fmt(corpLast)}`,
      住户中长期贷款: `当月 ${fmt(houseNow)} · 去年同期 ${fmt(houseLast)}`,
      财政性存款: `当月 ${fmt(fiscalNow)}`,
      口径: "当月增量 = 相邻两期累计相减；同比多增 = 当月增量 − 去年同月增量",
      来源: "中国人民银行《金融统计数据报告》分部门存贷款",
    },
  };
}

/** 并发抓取全部央行指标，返回 { records, errors }（失败项不入库）。*/
async function fetchAll() {
  const records = [];
  const errors = {};
  const addErr = (key, e) => { errors[key] = String(e && e.message ? e.message : e); };

  const tasks = [
    ["reverse_repo_rate", fetchReverseRepoRate],
    ["tsf_yoy", fetchTsfStockYoy],
  ];
  const settled = await Promise.allSettled(tasks.map(([, fn]) => fn()));
  tasks.forEach(([key], i) => {
    const s = settled[i];
    if (s.status === "fulfilled" && s.value && s.value.value != null) records.push(s.value);
    else addErr(key, s.status === "rejected" ? s.reason : new Error("无有效值"));
  });

  try {
    const cb = await fetchCreditBreakdown();
    const detail = cb.detail || {};
    for (const key of ["corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"]) {
      if (cb[key] != null) records.push({ key, value: cb[key], as_of: cb.as_of, source: SOURCE, detail });
      else errors[key] = "央行报告期数据不足，无法计算";
    }
  } catch (e) {
    for (const key of ["corp_ml_yoy", "household_ml_yoy", "fiscal_deposit_mom"]) addErr(key, e);
  }
  return { records, errors };
}

module.exports = { fetchAll, fetchReverseRepoRate, fetchTsfStockYoy, fetchCreditBreakdown };
