"use strict";
/** 东财新闻抓取：np-listapi getNewsByColumns column=350。*/
const client = require("./httpClient");
const { SOURCES } = require("./sources");
const { asJson } = require("./parsers");
const config = require("../config");

const COLUMN = 350;

async function fetchNews(pageSize) {
  const size = pageSize || config.newsPerSource || 20;
  const url =
    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns" +
    `?client=web&biz=web_news_col&column=${COLUMN}&order=1&needInteractData=0` +
    `&page_index=1&page_size=${size}&req_trace=${Date.now()}`;
  const r = await client.get(url, SOURCES.em_news, { noCache: true });
  const j = asJson(r.body);
  if (!j || String(j.code) !== "1") {
    throw new Error(`新闻接口返回异常 code=${j && j.code}`);
  }
  const list = (j.data && j.data.list) || [];
  return list.map((it) => ({
    title: it.title,
    url: it.uniqueUrl || it.url,
    publish_time: it.showTime,
    source: it.mediaName || "东方财富",
    summary: it.summary || "",
    content: it.content || null,
    author: it.author || null,
    image_path: it.image_path || it.imagePath || null,
  }));
}

module.exports = { fetchNews };
