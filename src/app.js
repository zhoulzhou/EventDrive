"use strict";
/** Express 应用装配：压缩、静态、模板、API 路由、页面路由、健康检查、404。*/
const express = require("express");
const compression = require("compression");
const nunjucks = require("nunjucks");
const config = require("./config");
const scheduler = require("./scheduler");

const pagesRouter = require("./routes/pages");
const newsRouter = require("./routes/news");
const marketRouter = require("./routes/market");
const macroRouter = require("./routes/macro");
const stockTempRouter = require("./routes/stockTemp");
const financeRouter = require("./routes/finance");
const valuationRouter = require("./routes/valuation");
const crawlRouter = require("./routes/crawl");

function createApp() {
  const app = express();

  app.use(compression());
  app.use(express.json({ limit: "1mb" }));
  app.use(express.urlencoded({ extended: true, limit: "1mb" }));

  nunjucks.configure(config.viewsDir, {
    autoescape: true,
    express: app,
    noCache: config.debug,
  });

  // 静态资源长缓存（调试期禁用）
  app.use(
    "/static",
    express.static(config.staticDir, {
      maxAge: config.debug ? 0 : "7d",
      immutable: !config.debug,
      fallthrough: true,
    })
  );

  app.get("/health", (req, res) => {
    let health = {};
    try { health = require("./services/health").getHealth(); } catch (_) { /* 只读，失败不影响健康检查 */ }
    res.json({
      status: "ok",
      app: config.appName,
      version: config.version,
      scheduler_running: scheduler.isRunning(),
      time: new Date().toISOString(),
      ...health,
    });
  });

  // API 路由
  app.use("/api", newsRouter);
  app.use("/api", marketRouter);
  app.use("/api", macroRouter);
  app.use("/api", stockTempRouter);
  app.use("/api", financeRouter);
  app.use("/api", valuationRouter);
  app.use("/api", crawlRouter);

  // 页面路由（必须在 API 之后，避免 /news/:id 之类吞掉 /api 前缀；API 已单独挂载）
  app.use("/", pagesRouter);

  // 根路径 → /home
  app.get("/", (req, res) => res.redirect(302, "/home"));

  // 404：/api 与 /static 保持 JSON；页面路径返回友好 HTML
  app.use((req, res) => {
    const p = req.path;
    if (p.startsWith("/api") || p.startsWith("/static")) {
      return res.status(404).json({ detail: "Not Found", path: p });
    }
    res.status(404).type("html").send(
      `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">` +
        `<meta name="viewport" content="width=device-width,initial-scale=1"><title>404</title>` +
        `<link rel="stylesheet" href="/static/css/style.css"></head><body>` +
        `<div class="container"><div class="error-state"><h1>404</h1>` +
        `<p>没有这个页面：<code>${p}</code></p>` +
        `<p><a class="btn" href="/home">返回首页</a></p></div></div></body></html>`
    );
  });

  return app;
}

module.exports = { createApp };
