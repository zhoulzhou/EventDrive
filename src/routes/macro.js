"use strict";
/** 宏观指标 API：展示 / 历史序列 / 手动刷新。无历史回填路由（AC-6.4）。无鉴权。*/
const express = require("express");
const router = express.Router();
const service = require("../services/macroService");

router.get("/macro", (_req, res) => {
  try {
    res.json(service.getDisplay());
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.get("/macro/history", (req, res) => {
  const key = String(req.query.key || "");
  const limit = Math.min(parseInt(req.query.limit, 10) || 200, 2000);
  const order = req.query.order === "asc" ? "asc" : "desc";
  try {
    res.json(service.getHistory(key, limit, order));
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.post("/macro/refresh", async (_req, res) => {
  try {
    res.json(await service.refresh());
  } catch (e) {
    res.status(502).json({ status: "error", message: String((e && e.message) || e) });
  }
});

module.exports = router;
