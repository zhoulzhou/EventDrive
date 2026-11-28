"use strict";
/** 页面路由：9 个页面（无登录）。当前为占位渲染，各业务任务会替换为真实模板。*/
const express = require("express");
const router = express.Router();

const PAGES = [
  ["/home", "首页", "新闻与数据总览"],
  ["/market", "全球行情", "全球市场行情数据"],
  ["/stock-temp", "股市热度", "A 股冷热温度计"],
  ["/stock-trend", "热度趋势", "热度指标历史趋势"],
  ["/macro", "宏观指标", "宏观经济指标"],
  ["/finance", "财务指标", "上市公司财务三表"],
  ["/valuation", "公司估值", "DCF 估值计算"],
  ["/crawl", "抓取控制", "数据源抓取控制"],
];

for (const [p, title, msg] of PAGES) {
  if (p === "/home") {
    router.get(p, (req, res) => res.render("index.html"));
  } else if (p === "/market") {
    router.get(p, (req, res) => res.render("market.html"));
  } else if (p === "/macro") {
    router.get(p, (req, res) => res.render("macro.html"));
  } else if (p === "/stock-temp") {
    router.get(p, (req, res) => res.render("stock_temp.html"));
  } else if (p === "/stock-trend") {
    router.get(p, (req, res) => res.render("stock_trend.html"));
  } else if (p === "/crawl") {
    router.get(p, (req, res) => res.render("crawl_control.html"));
  } else if (p === "/finance") {
    router.get(p, (req, res) => res.render("finance.html"));
  } else if (p === "/valuation") {
    router.get(p, (req, res) => res.render("valuation.html"));
  } else {
    router.get(p, (req, res) => res.render("_placeholder.html", { title, msg }));
  }
}
router.get("/news/:id", (req, res) =>
  res.render("news_detail.html", { news_id: req.params.id })
);

module.exports = router;
