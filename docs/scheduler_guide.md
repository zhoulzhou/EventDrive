# Scheduler 执行逻辑总结

## 定时任务

### 新闻抓取
每 3 小时一次：`00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00`

### 指数监控
每小时一次：`*:00`

## 新闻源顺序（4个源串行执行）

| 顺序 | 新闻源 | Crawler | 飞书推送函数 | 大模型分析 | 提示语 |
|------|--------|---------|-------------|-----------|--------|
| 1 | 东方财富 | `EastmoneyDepthCrawler` | `dfcf_feishu_notify` | 豆包 | 中文 |
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

## 指数监控（crawl_indices）

### 功能
- 使用腾讯财经API获取 NDX 和 VIX 指数
- 追踪年内高点（初始值 26011.75）
- 推送条件：
  - NDX 突破历史新高：发送"🎉 突破历史新高!"提示
  - NDX 偏离高点 ≥10%/5%/3%：发送偏离预警
  - NDX 下跌 ≥30%/20%/10%/5%/3%：发送下跌警报
  - VIX ≥ 25/30：发送恐慌预警

### 飞书推送
- 使用 `notify_index_alert` 发送
- 飞书 Webhook: `INDEX_FEISHU_WEBHOOK_URL`
- 关键词: `INDEX_KEYWORD`

## 初始化

### 大模型分析器（在 scheduler.py full_crawl 中）
- 豆包分析器：使用 `KB_API_KEY`, `KB_MODEL_ID`, `KB_REGION` 配置
- OpenRouter分析器：使用 `OPENROUTER_API_KEY` 配置

### 飞书推送（在 main.py 启动时）
- `init_all_notifiers()` — 统一初始化所有飞书推送（feishu, nyt, bbc, em, index）

## 关键文件

| 文件 | 功能 |
|------|------|
| `app/scheduler.py` | 主编排逻辑 |
| `app/utils/feishu_notifier.py` | 所有飞书推送 |
| `app/utils/doubao_analyzer.py` | 豆包分析（只分析不推送） |
| `app/utils/openrouter_analyzer.py` | OpenRouter 分析（只分析不推送） |
| `app/crawlers/finnhub_index.py` | 指数获取（腾讯财经API） |
| `app/main.py` | 启动时统一初始化飞书 |

## 飞书推送通道

| 通道 | 函数 | 用途 |
|------|------|------|
| 豆包飞书 | `doubao_feishu_notify` | 东方财富、财联社分析结果推送 |
| OpenRouter飞书 | `openrouter_feishu_notify` | 纽约时报、BBC分析结果推送 |
| 指数飞书 | `notify_index_alert` | 指数监控结果推送 |
| 东方财富飞书 | `dfcf_feishu_notify` | 东方财富原始新闻推送 |
| 财联社飞书 | `cls_feishu_notify` | 财联社原始新闻推送 |
| 纽约时报飞书 | `nyt_feishu_notify` | 纽约时报原始新闻推送 |
| BBC飞书 | `bbc_feishu_notify` | BBC原始新闻推送 |

## 大模型分析

### 分析输入
- 标题（`title`）
- 摘要（`summary`）

### 豆包分析（中文）
- 宏观投资环境影响
- 整体股票市场影响
- 相关上市公司影响（受益/受损）
- 投资操作建议（仓位/行业/个股/风险）

### OpenRouter分析（英文，美股市场）
- Macro Investment Environment Impact
- Overall Stock Market Impact
- Related Companies Impact
- Investment Operation Suggestions

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

### 指数获取失败
1. 检查腾讯财经API `qt.gtimg.cn` 是否可访问
2. 查看日志中是否有解析错误
3. 检查 `NDX_INITIAL_HIGH` 配置是否正确
