"use strict";
/** 财务三表 API：股票列表 / 查询 / 抓取入库。无鉴权。无历史回填（AC-12）。*/
const express = require("express");
const router = express.Router();
const service = require("../services/financeService");

router.get("/finance/stocks", (_req, res) => {
  try {
    res.json({ status: "ok", stocks: service.listStocks() });
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.get("/finance/query", (req, res) => {
  try {
    const r = service.query(req.query.code, req.query.name, req.query.start, req.query.end);
    res.json({ status: "ok", ...r });
  } catch (e) {
    const status = (e && e.status) || 500;
    res.status(status).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.post("/finance/fetch", async (req, res) => {
  try {
    const r = await service.fetch(req.query.code, req.query.name, req.query.start, req.query.end);
    res.json({
      status: "ok",
      code: req.query.code,
      name: r.stockName,
      saved: r.saved,
      fields: r.fields,
      records: r.records,
    });
  } catch (e) {
    const status = (e && e.status) || 502;
    res.status(status).json({ status: "error", message: String((e && e.message) || e) });
  }
});

module.exports = router;
