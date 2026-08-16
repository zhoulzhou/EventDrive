# Scheduler 执行逻辑总结

## 定时任务

### 新闻抓取
每 3 小时一次：`00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00`

### 市场行情
每日 `08:00, 12:00, 16:00, 20:00`（JST，Asia/Tokyo）

## 新闻源顺序（4个源串行执行）

| 顺序 | 新闻源 | Crawler | 飞书推送函数 | 大模型分析 | 提示语 |
|------|--------|---------|-------------|-----------|--------|
| 1 | 东方财富 | `EastmoneyDepthCrawler` | `dfcf_feishu_notify` | DeepSeek+豆包 | 中文 |
| 2 | 财联社 | `CLSDepthCrawler` | `cls_feishu_notify` | 豆包 | 中文 |
| 3 | 纽约时报 | `NYTDepthCrawler` | `nyt_feishu_notify` | OpenRouter | **英文** |
| 4 | BBC | `BBCCrawler` | `bbc_feishu_notify` | OpenRouter | **英文** |

## 每个新闻源的执行流程

```
for each 新闻源:
    1. crawl_single_source(crawler_class)           # 抓取新闻
    2. xxx_feishu_notify(saved_news[:5])             # 飞书推送原始新闻（前5条）
    3. 如果分析器可用:
        for news in saved_news[:2]:                  # 大模型分析（每源取前2条）
            - analyze_only(title, summary, source)   # 传入标题+摘要进行分析
            - xxx_feishu_notify(title, result)       # 推送分析结果
```

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

### 大模型分析器（在 scheduler.py full_crawl 中）
- 豆包分析器：使用 `KB_API_KEY`, `KB_MODEL_ID`, `KB_REGION` 配置
- OpenRouter分析器：使用 `OPENROUTER_API_KEY` 配置，`model="openrouter/free"` 自动路由免费模型

### 飞书推送（在 main.py 启动时）
- `init_all_notifiers()` — 统一初始化所有飞书推送（feishu, nyt, bbc, em, index）

## 关键文件

| 文件 | 功能 |
|------|------|
| `app/scheduler.py` | 主编排逻辑 |
| `app/utils/feishu_notifier.py` | 所有飞书推送 |
| `app/utils/doubao_analyzer.py` | 豆包分析（只分析不推送） |
| `app/utils/openrouter_analyzer.py` | OpenRouter 分析（只分析不推送） |
| `app/crawlers/market_data.py` | 市场行情获取（FRED） |
| `app/main.py` | 启动时统一初始化飞书 |

## 飞书推送通道

| 通道 | 函数 | 用途 |
|------|------|------|
| 豆包飞书 | `doubao_feishu_notify` | 东方财富、财联社分析结果推送 |
| OpenRouter飞书 | `openrouter_feishu_notify` | 纽约时报、BBC分析结果推送 |
| 东方财富飞书 | `dfcf_feishu_notify` | 东方财富原始新闻推送 |
| 财联社飞书 | `cls_feishu_notify` | 财联社原始新闻推送 |
| 纽约时报飞书 | `nyt_feishu_notify` | 纽约时报原始新闻推送 |
| BBC飞书 | `bbc_feishu_notify` | BBC原始新闻推送 |

## 大模型分析

### 分析输入
- 标题（`title`）
- 摘要（`summary`）

### 豆包分析（中文，使用 DeepSeek 风格提示词）
- 核心事件概括
- 关键影响（宏观/行业/资本市场）
- 市场情绪判断
- 风险提示

### OpenRouter分析（英文，使用 DeepSeek 风格提示词）
- Core Event Summary
- Key Impact (Macro / Industry / Capital Market)
- Market Sentiment Analysis
- Risk Warning

## 常见问题排查

### 推送失败
1. 检查飞书推送函数是否正确导入
2. 检查 `_feishu_notifier` 是否已初始化（main.py 启动时）
3. 查看日志中是否有 "飞书未初始化" 警告
4. 检查飞书 webhook URL 是否正确配置

### 分析失败
1. 检查 `analyze_only` 返回值是否为 None
2. 查看 `analyze_news` 函数的日志输出
3. 检查 API Key 是否有效
4. 检查网络连接和大模型 API 限流

### 市场行情获取失败
1. 检查 FRED 接口 `fred.stlouisfed.org` 是否可访问
2. 查看日志中是否有请求超时或解析错误
3. 确认 `market_prices` 表是否有数据（首次运行需等待调度触发或页面兜底抓取）
