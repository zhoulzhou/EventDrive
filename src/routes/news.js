"use strict";
/** 新闻 API：列表、详情、手动刷新。无鉴权。*/
const express = require("express");
const router = express.Router();
const service = require("../services/newsService");

// 解析逗号分隔关键词
function splitKw(s) {
  if (!s) return null;
  return s.split(",").map((k) => k.trim()).filter(Boolean);
}

router.get("/news", (req, res) => {
  const skip = Math.max(0, parseInt(req.query.skip, 10) || 0);
  let limit = parseInt(req.query.limit, 10);
  if (!Number.isFinite(limit) || limit < 1) limit = 100;
  limit = Math.min(limit, 1000); // 对齐原接口上限
  const opts = {
    skip,
    limit,
    source: req.query.source || undefined,
    include_keywords: splitKw(req.query.include_keywords),
    exclude_keywords: splitKw(req.query.exclude_keywords),
  };
  const list = service.getList(opts);
  res.json({ news: list, total: service.count() });
});

router.get("/news/:id", (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isFinite(id)) return res.status(400).json({ detail: "invalid id" });
  const item = service.getDetail(id);
  if (!item) return res.status(404).json({ detail: "News not found" });
  res.json(item);
});

router.post("/news/refresh", async (req, res) => {
  try {
    const r = await service.fetchAndStore();
    res.json({ ok: true, ...r });
  } catch (e) {
    res.status(502).json({ ok: false, detail: String(e && e.message || e) });
  }
});

module.exports = router;
