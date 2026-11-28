"use strict";
/** DCF 估值 API：计算入库 / 历史列表 / 删除单条 / 清空。无鉴权。*/
const express = require("express");
const router = express.Router();
const service = require("../services/valuationService");

router.post("/valuation/calculate", (req, res) => {
  try {
    const r = service.calculateAndStore(req.body || {});
    res.json({ status: "ok", ...r });
  } catch (e) {
    const status = (e && e.status) || 500;
    res.status(status).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.get("/valuation/history", (req, res) => {
  try {
    res.json({ status: "ok", history: service.listHistory(req.query.limit) });
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.delete("/valuation/history", (_req, res) => {
  try {
    res.json({ status: "ok", deleted: service.clearAll() });
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.delete("/valuation/:record_id", (req, res) => {
  try {
    res.json({ status: "ok", deleted: service.deleteById(req.params.record_id) });
  } catch (e) {
    const status = (e && e.status) || 500;
    res.status(status).json({ status: "error", message: String((e && e.message) || e) });
  }
});

module.exports = router;
