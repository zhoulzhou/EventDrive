"use strict";
/** 新闻服务：抓取→去重入库、列表、详情。失败抛错由调用方处理（不写兜底）。*/
const repo = require("../db/news");
const fetcher = require("../fetchers/newsEastmoney");

async function fetchAndStore(pageSize) {
  const items = await fetcher.fetchNews(pageSize);
  let inserted = 0, updated = 0;
  for (const it of items) {
    if (!it.url || !it.title) continue;
    const res = repo.upsertNews(it);
    if (res.inserted) inserted++; else updated++;
  }
  return { total: items.length, inserted, updated };
}

function getList(opts) {
  return repo.listNews(opts);
}
function getDetail(id) {
  return repo.getNewsById(id);
}
function count() {
  return repo.countNews();
}

module.exports = { fetchAndStore, getList, getDetail, count };
