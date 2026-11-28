"use strict";
/** 市场行情 API。无鉴权。*/
const express = require("express");
const router = express.Router();
const service = require("../services/marketService");

router.get("/market", async (req, res) => {
  try {
    res.json(await service.getDisplay());
  } catch (e) {
    res.status(500).json({ status: "error", message: String(e && e.message || e) });
  }
});

router.get("/market/history", async (req, res) => {
  const symbol = req.query.symbol;
  if (!symbol) return res.status(400).json({ status: "error", message: "symbol required" });
  try {
    res.json(await service.getHistory(symbol));
  } catch (e) {
    res.status(500).json({ status: "error", message: String(e && e.message || e) });
  }
});

router.post("/market/refresh", async (req, res) => {
  try {
    res.json(await service.fetchAndStore());
  } catch (e) {
    res.status(502).json({ status: "error", message: String(e && e.message || e) });
  }
});

router.post("/market/cleanup", async (req, res) => {
  try {
    const deleted = service.cleanupSparse();
    res.json({ status: "ok", deleted });
  } catch (e) {
    res.status(500).json({ status: "error", message: String(e && e.message || e) });
  }
});

module.exports = router;
