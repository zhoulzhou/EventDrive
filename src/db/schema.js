"use strict";
/** 建表 DDL，对齐原 app/models.py 的 9 张表。仅建表，不写任何种子/兜底数据。*/

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT,
  source TEXT NOT NULL,
  publish_time TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  author TEXT,
  summary TEXT,
  image_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_news_publish_time ON news(publish_time);
CREATE INDEX IF NOT EXISTS ix_news_source ON news(source);

CREATE TABLE IF NOT EXISTS market_prices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  unit TEXT,
  value REAL,
  date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_market_prices_symbol ON market_prices(symbol);

CREATE TABLE IF NOT EXISTS market_strategy_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL UNIQUE,
  peak_value REAL NOT NULL,
  peak_date TEXT NOT NULL,
  drawdown_date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_valuations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_name TEXT NOT NULL,
  base_profit REAL NOT NULL,
  forecast_years INTEGER NOT NULL,
  growth_forecast REAL NOT NULL,
  transition_years INTEGER,
  growth_transition REAL,
  growth_perpetual REAL NOT NULL,
  discount_rate REAL NOT NULL,
  current_market_value REAL,
  enterprise_value REAL NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS macro_indicators (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  value REAL,
  as_of TEXT,
  source TEXT NOT NULL DEFAULT 'fetcher',
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS macro_indicator_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value REAL,
  as_of TEXT,
  source TEXT NOT NULL DEFAULT 'fetcher',
  detail TEXT,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_macro_hist_key ON macro_indicator_history(key);
CREATE INDEX IF NOT EXISTS ix_macro_hist_fetched ON macro_indicator_history(fetched_at);

CREATE TABLE IF NOT EXISTS stock_temp_indicators (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,
  value REAL,
  as_of TEXT,
  source TEXT NOT NULL DEFAULT 'fetcher',
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_temp_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  value REAL,
  as_of TEXT,
  source TEXT NOT NULL DEFAULT 'fetcher',
  detail TEXT,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_stocktemp_hist_key ON stock_temp_history(key);
CREATE INDEX IF NOT EXISTS ix_stocktemp_hist_fetched ON stock_temp_history(fetched_at);

CREATE TABLE IF NOT EXISTS financial_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT NOT NULL,
  stock_name TEXT,
  report_date TEXT NOT NULL,
  revenue REAL,
  operating_cost REAL,
  net_profit REAL,
  inventory REAL,
  accounts_receivable REAL,
  cash REAL,
  short_term_investment REAL,
  contract_liabilities REAL,
  shareholders_equity REAL,
  operating_cash_flow REAL,
  short_term_borrowing REAL,
  non_current_liab_due_1y REAL,
  long_term_borrowing REAL,
  bonds_payable REAL,
  interest_expense REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(stock_code, report_date)
);
CREATE INDEX IF NOT EXISTS ix_financial_stock_code ON financial_reports(stock_code);
`;

function initSchema(db) {
  db.exec(SCHEMA_SQL);
}

module.exports = { initSchema, SCHEMA_SQL };
