"use strict";
/** 数据源 Header 注册表。每个源用正确的 UA + Referer，降低反爬命中。*/

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36";

const SOURCES = {
  // 东财 push2 实时行情 / K线
  em_quote: {
    "User-Agent": UA,
    Accept: "application/json, text/plain, */*",
    Referer: "https://quote.eastmoney.com/",
  },
  // 东财 datacenter-web 报表
  em_data: {
    "User-Agent": UA,
    Referer: "https://data.eastmoney.com/",
  },
  // 东财新闻
  em_news: {
    "User-Agent": UA,
    Accept: "application/json, text/plain, */*",
    Referer: "https://finance.eastmoney.com/a/ccjdd.html",
  },
  // 东财基金
  em_fund: {
    "User-Agent": UA,
    Referer: "https://fund.eastmoney.com/",
  },
  // 新浪行情（BTC / 伦敦金）
  sina: {
    "User-Agent": UA,
    Referer: "https://finance.sina.com.cn",
  },
  // CBOE（VIX CSV）
  cboe: {
    "User-Agent": UA,
    Accept: "text/csv, application/octet-stream, */*",
  },
  // 美国财政部（美债收益率 CSV）
  treasury: {
    "User-Agent": UA,
    Accept: "text/csv, */*",
  },
  // 中国人民银行官网（社融 / 逆回购）
  pbc: {
    "User-Agent": UA,
    Accept: "text/html, */*",
  },
};

module.exports = { UA, SOURCES };
