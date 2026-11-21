# Scheduler 执行逻辑总结

## 定时任务

### 新闻抓取
每日 `08:00, 12:00, 16:00, 20:00`（JST，Asia/Tokyo）

### 市场行情
每日 `08:00, 12:00, 16:00, 20:00`（JST，Asia/Tokyo）

## 新闻源（仅东方财富）

| 顺序 | 新闻源 | Crawler | 提示语 |
|------|--------|---------|--------|
| 1 | 东方财富 | `EastmoneyDepthCrawler`（新闻列表接口） | 中文 |

## 新闻抓取流程

```
for each 新闻源:
    crawl_single_source(crawler_class)   # 抓取新闻并写入数据库
```

抓取结果仅入库，不做飞书推送，也不调用大模型分析。

## 市场行情（crawl_market_data）

### 功能
- 使用海外数据源 FRED（美国圣路易斯联储）获取 4 项指标
- 纳斯达克指数（`NASDAQCOM`）、VIX恐慌指数（`VIXCLS`）
- 美债2年期收益率（`DGS2`）、美债10年期收益率（`DGS10`）
- 抓取结果写入 `market_prices` 表（定时落库），页面 `/api/market` 从数据库读取

### 流程
- 调度器触发 `crawl_market_data()` → `refresh_market_data()`（异步）
- `fetch_fred_prices()` 逐个异步请求 FRED CSV 序列
- `save_market_prices()` 通过 `asyncio.to_thread` 写入数据库，避免阻塞事件循环
- 页面首次访问且库为空时，`/api/market` 兜底实时抓取一次

## 初始化

- `main.py` / `run_scheduler.py` 只负责创建数据表、启动调度器，不再初始化推送或分析组件

## 关键文件

| 文件 | 功能 |
|------|------|
| `app/scheduler.py` | 主编排逻辑 |
| `app/crawlers/eastmoney_depth.py` | 东方财富新闻接口抓取 |
| `app/crawlers/market_data.py` | 市场行情获取（FRED） |
| `app/main.py` | Web 启动入口 |

## 常见问题排查

### 新闻抓取失败
1. 检查东方财富接口 `np-listapi.eastmoney.com` 是否可访问
2. 查看日志中是否有请求超时或解析错误
3. 确认 `NEWS_PER_SOURCE`、`NEWS_TIME_RANGE_HOURS` 配置是否合理

### 市场行情获取失败
1. 检查 FRED 接口 `fred.stlouisfed.org` 是否可访问
2. 查看日志中是否有请求超时或解析错误
3. 确认 `market_prices` 表是否有数据（首次运行需等待调度触发或页面兜底抓取）
