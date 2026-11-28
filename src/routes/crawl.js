"use strict";
/** 抓取控制 API：调度状态 / 手动触发。无鉴权、无历史回溯。*/
const express = require("express");
const router = express.Router();
const scheduler = require("../scheduler");

router.get("/crawl/status", (_req, res) => {
  try {
    res.json(scheduler.status());
  } catch (e) {
    res.status(500).json({ status: "error", message: String((e && e.message) || e) });
  }
});

router.post("/crawl/trigger", (req, res) => {
  const job = String(req.query.job || (req.body && req.body.job) || "news");
  try {
    const result = scheduler.trigger(job);
    res.json(result);
  } catch (e) {
    res.status(400).json({ status: "error", message: String((e && e.message) || e) });
  }
});

module.exports = router;
