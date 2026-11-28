"use strict";
/** 进程入口：初始化数据库 → 启动调度器 → 监听 PORT。*/
const config = require("./config");
const { openDb, getDb } = require("./db");
const { createApp } = require("./app");
const scheduler = require("./scheduler");

(async () => {
  console.log("=".repeat(60));
  console.log("🚀 EventDrive (Node.js) 正在启动...");
  console.log("=".repeat(60));

  await openDb();
  console.log("✅ 数据库初始化完成（无种子/兜底写入）");

  if (config.startScheduler) {
    scheduler.start();
    console.log("✅ 调度器已启动");
  } else {
    console.log("ℹ️  调度器未启动（START_SCHEDULER=false）");
  }

  const app = createApp();
  const server = app.listen(config.port, config.host, () => {
    console.log(`✅ 服务已启动: http://${config.host}:${config.port}`);
    console.log(`   健康检查: http://${config.host}:${config.port}/health`);
  });

  const shutdown = (sig) => {
    console.log(`\n收到 ${sig}，正在关闭...`);
    scheduler.stop();
    try { getDb().close(); } catch (_) {}
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
})().catch((e) => {
  console.error("❌ 启动失败:", e);
  process.exit(1);
});
