"use strict";
/**
 * 新浪财经财务三表抓取（HTML，GBK 编码 → iconv 解码）。
 *
 * 多源选型：东方财富 F10 详表 AJAX（emweb）对部分标的返回「无F10资料」，
 * 且 RPT_DMSK_FN_* 为汇总/比率表、缺少明细科目（交易性金融资产/合同负债/
 * 一年内到期非流动负债/长期借款/应付债券/利息支出），故采用新浪原始三表。
 *
 * 纪律（AC-12）：拉不到的指标保持 null，绝不写回退默认值。
 */
const iconv = require("iconv-lite");
const client = require("./httpClient");
const { SOURCES } = require("./sources");

// 显示名 -> 新浪报表原始行标签（与 getAkshare.py FIELD_MAP 完全一致）
const FIELD_MAP = {
  营业收入: "营业收入",
  营业成本: "营业成本",
  归母净利润: "归属于母公司所有者的净利润",
  存货: "存货",
  应收账款: "应收账款",
  货币资金: "货币资金",
  短期理财: "交易性金融资产",
  合同负债: "合同负债",
  股东权益: "所有者权益(或股东权益)合计",
  经营活动现金流净额: "经营活动产生的现金流量净额",
  短期借款: "短期借款",
  一年内到期的非流动负债: "一年内到期的非流动负债",
  长期借款: "长期借款",
  应付债券: "应付债券",
  利息支出: "利息支出",
};

const STATEMENTS = {
  profit: "vFD_ProfitStatement",
  balance: "vFD_BalanceSheet",
  cashflow: "vFD_CashFlow",
};

function decodeGbk(buf) {
  return Buffer.isBuffer(buf) ? iconv.decode(buf, "gbk") : String(buf);
}

function cellText(raw) {
  return raw
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// 新浪报表单位为「万元」，统一换算为「元」入库（与 akshare 口径一致，
// 后续 financeIndicators 按 元→亿元 /1e8 换算）。
const WAN_TO_YUAN = 1e4;

function parseNum(v) {
  if (!v) return null;
  const s = String(v).trim();
  if (s === "--" || s === "-" || s === "" || s.toLowerCase() === "null") return null;
  const n = Number(s.replace(/,/g, ""));
  return Number.isFinite(n) ? n * WAN_TO_YUAN : null;
}

/** 解析单张新浪报表 HTML → { dates:[], rows: { label: { date: number|null } } } */
function parseStatement(html) {
  const rows = [];
  const trRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
  let tm;
  while ((tm = trRe.exec(html))) {
    const cells = [];
    const tdRe = /<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi;
    let td;
    while ((td = tdRe.exec(tm[1]))) cells.push(cellText(td[1]));
    if (cells.length) rows.push(cells);
  }
  let dates = null;
  let start = -1;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r[0] === "报表日期" && r.slice(1).some((c) => /^\d{4}-\d{2}-\d{2}$/.test(c))) {
      dates = r.slice(1).map((c) => c.trim()).filter(Boolean);
      start = i + 1;
      break;
    }
  }
  if (!dates) throw new Error("未找到「报表日期」表头行");
  const out = {};
  for (let i = start; i < rows.length; i++) {
    const r = rows[i];
    if (r.length < 2 || !r[0]) continue;
    out[r[0]] = {};
    dates.forEach((d, j) => {
      out[r[0]][d] = parseNum(r[j + 1] || "");
    });
  }
  return { dates, rows: out };
}

function extractStockName(html) {
  // 表头形如：贵州茅台(600519) 资产负债表
  const m = html.match(/<th[^>]*>([\s\S]*?)<\/th>/i);
  if (!m) return null;
  const t = cellText(m[1]);
  const mm = t.match(/^(.+?)\s*\(/);
  return mm ? mm[1].trim() : null;
}

/**
 * 抓取三大报表并按报告期内连接合并。
 * @returns {Promise<{records:Array<object>, stockName:string|null}>}
 *   records 为显示名键：{ 报告期, 营业收入, ... }，按报告期升序去重。
 */
async function fetchFinancialReports(code) {
  const parsed = {};
  let stockName = null;
  for (const path of Object.values(STATEMENTS)) {
    const url = `https://money.finance.sina.com.cn/corp/go.php/${path}/stockid/${code}/ctrl/part/displaytype/4.phtml`;
    const r = await client.get(url, SOURCES.sina, { raw: true, noCache: true });
    if (r.status < 200 || r.status >= 300) throw new Error(`新浪财报 HTTP ${r.status}`);
    const html = decodeGbk(r.body);
    if (!stockName) stockName = extractStockName(html);
    parsed[path] = parseStatement(html);
  }
  const tables = Object.values(parsed);
  if (!tables.length) return { records: [], stockName };

  const dateSets = tables.map((t) => new Set(t.dates));
  // 三张表都存在的报告期（内连接），保持第一张表的列序后再升序排序
  const common = tables[0].dates.filter((d) => dateSets.every((s) => s.has(d)));
  const seen = new Set();
  const records = [];
  for (const d of [...common].sort()) {
    if (seen.has(d)) continue;
    seen.add(d);
    const rec = { 报告期: d };
    for (const [disp, raw] of Object.entries(FIELD_MAP)) {
      let v = null;
      for (const t of tables) {
        if (t.rows[raw] && t.rows[raw][d] != null) {
          v = t.rows[raw][d];
          break;
        }
      }
      rec[disp] = v;
    }
    records.push(rec);
  }
  return { records, stockName };
}

module.exports = { fetchFinancialReports, FIELD_MAP };
