"use strict";
/**
 * 财务三表 repo：按 (stock_code, report_date) upsert。
 * 字段口径对齐原 crud.py 的 FINANCIAL_FIELD_COLUMNS（展示顺序）。
 */
const { getDb } = require("./index");

const FINANCIAL_FIELDS = [
  { display: "营业收入", col: "revenue" },
  { display: "营业成本", col: "operating_cost" },
  { display: "归母净利润", col: "net_profit" },
  { display: "存货", col: "inventory" },
  { display: "应收账款", col: "accounts_receivable" },
  { display: "货币资金", col: "cash" },
  { display: "短期理财", col: "short_term_investment" },
  { display: "合同负债", col: "contract_liabilities" },
  { display: "股东权益", col: "shareholders_equity" },
  { display: "经营活动现金流净额", col: "operating_cash_flow" },
  { display: "短期借款", col: "short_term_borrowing" },
  { display: "一年内到期的非流动负债", col: "non_current_liab_due_1y" },
  { display: "长期借款", col: "long_term_borrowing" },
  { display: "应付债券", col: "bonds_payable" },
  { display: "利息支出", col: "interest_expense" },
];

const COLS = FINANCIAL_FIELDS.map((f) => f.col).join(", ");
const UPSERT_SQL = `INSERT INTO financial_reports (stock_code, stock_name, report_date, ${COLS})
  VALUES (?,?,?, ${FINANCIAL_FIELDS.map(() => "?").join(", ")})
  ON CONFLICT(stock_code, report_date) DO UPDATE SET
    ${FINANCIAL_FIELDS.map((f) => `${f.col}=excluded.${f.col}`).join(", ")},
    stock_name=excluded.stock_name, updated_at=datetime('now')`;

/** records: [{报告期, 营业收入, ...}]  display-name 键。返回写入条数。*/
function upsertReports(stockCode, records, stockName) {
  if (!records || !records.length) return 0;
  const db = getDb();
  return db.transaction(() => {
    const stmt = db.prepare(UPSERT_SQL);
    let n = 0;
    for (const rec of records) {
      const vals = [stockCode, stockName ?? null, rec["报告期"] ?? null];
      for (const f of FINANCIAL_FIELDS) vals.push(rec[f.display] ?? null);
      stmt.run(...vals);
      n++;
    }
    return n;
  });
}

function listReports(stockCode) {
  const db = getDb();
  return db
    .prepare(
      `SELECT stock_code, stock_name, report_date, ${COLS}, created_at
       FROM financial_reports WHERE stock_code=? ORDER BY report_date ASC`
    )
    .all(stockCode);
}

function listCodes() {
  const db = getDb();
  return db
    .prepare(
      `SELECT DISTINCT stock_name AS name, stock_code AS code FROM financial_reports
       WHERE stock_name IS NOT NULL ORDER BY stock_name ASC`
    )
    .all();
}

module.exports = { upsertReports, listReports, listCodes, FINANCIAL_FIELDS };
