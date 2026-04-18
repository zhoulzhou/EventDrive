# Scheduler 执行逻辑总结

## 执行时间
每 3 小时一次：`00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00`

## 新闻源顺序（4个源串行执行）

| 顺序 | 新闻源 | Crawler | 飞书推送函数 | 大模型 |
|------|--------|---------|-------------|--------|
| 1 | 东方财富 | `EastmoneyDepthCrawler` | `notify_em_news` | 豆包 |
| 2 | 财联社 | `CLSDepthCrawler` | `notify_new_news` | OpenRouter |
| 3 | 纽约时报 | `NYTDepthCrawler` | `notify_nyt_news` | 豆包 |
| 4 | BBC | `BBCCrawler` | `notify_bbc_news` | OpenRouter |

## 每个新闻源的执行流程

```
for each 新闻源:
    1. crawl_single_source(crawler_class)  # 抓取新闻
    2. notify_func(saved_news[:5])          # 飞书推送原始新闻
    3. analyzer.analyze_only()             # 大模型分析（每源取前2条）
    4. send_analysis_to_feishu()           # 飞书推送分析结果
```

## 初始化（启动时 main.py）
- `init_all_notifiers()` — 统一初始化所有飞书推送（feishu, nyt, bbc, em, index）
- 飞书推送带 30 秒冷却限制，消息入队列缓存

## 关键文件
- `app/scheduler.py` — 主编排逻辑
- `app/utils/feishu_notifier.py` — 所有飞书推送（含 30 秒冷却队列）
- `app/utils/knowledge_analyzer.py` — OpenRouter 分析（只分析不推送）
- `app/utils/doubao_analyzer.py` — 豆包分析（只分析不推送）
- `app/main.py` — 启动时统一初始化飞书

## 飞书推送冷却机制
- 30 秒内只允许一次推送
- 超过限制的消息缓存到 `_pending_queue`
- 后台线程等待冷却后逐个发送缓存消息

## 常见问题排查

### 推送失败
1. 检查 `send_analysis_to_feishu` 函数是否正确导入
2. 检查 `_feishu_notifier` 是否已初始化（main.py 启动时）
3. 查看日志中是否有 "飞书未初始化" 警告
4. 检查飞书 webhook URL 是否正确配置

### 分析失败
1. 检查 `analyze_only` 返回值是否为 None
2. 查看 `analyze_news` 函数的日志输出
3. 检查 API Key 是否有效
4. 检查网络连接和大模型 API 限流

### 冷却限制问题
1. 消息被缓存但未发送 → 等待后台线程 drain
2. 消息被丢弃 → 当前实现不会丢弃，已缓存等待冷却后发送
3. 多个 drain 线程重复发送 → 有 `_draining` 标志防止重复启动
