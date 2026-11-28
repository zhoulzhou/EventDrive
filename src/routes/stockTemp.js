"use strict";
/** 股市温度 API：展示 / 序列 / 历史明细 / 手动刷新。无历史回填路由（AC-12）。无鉴权。*/
const express = require("express");
const router = express.Router();
const service = require("../services/stockTempService");

router.get("/stock-temp", (_req, res) => {
  try {
    res.json(service.getDisplay());
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.get("/stock-temp/series", (_req, res) => {
  try {
    res.json(service.getSeries());
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.get("/stock-temp/history", (req, res) => {
  const key = String(req.query.key || "");
  const limit = Math.min(parseInt(req.query.limit, 10) || 200, 2000);
  const order = req.query.order === "asc" ? "asc" : "desc";
  try {
    res.json(service.getHistory(key, limit, order));
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.post("/stock-temp/refresh", async (_req, res) => {
  try {
    res.json(await service.refresh());
  } catch (e) {
    res.status(502).json({ status: "error", message: String((e && e.message) || e) });
  }
});

module.exports = router;
