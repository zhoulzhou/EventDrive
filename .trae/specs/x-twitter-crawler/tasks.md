# Tasks

- [x] Task 1: 新增 X 平台配置项
  - [x] 在 `app/config.py` 中添加 X API 配置项（CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET）
  - [x] 添加 X 飞书推送配置项（X_FEISHU_WEBHOOK_URL, X_FEISHU_KEYWORD）
  - [x] 添加抓取额度配置项（X_MAX_RESULTS, X_MONTH_MAX_LIMIT, X_DAY_MAX_LIMIT）
  - [x] 在 `.env.example` 中添加对应示例

- [x] Task 2: 实现 X 推文爬虫核心逻辑
  - [x] 新建 `app/crawlers/x_twitter.py`，实现 X 推文爬虫
  - [x] 实现持久化状态管理函数（last_tweet_id.json、x_month_count.json、x_day_count.json 读写）
  - [x] 实现月度/当日额度检查拦截逻辑
  - [x] 实现增量抓取核心逻辑（since_id 机制，max_results 限制，不包含 expansions）
  - [x] 实现额度超限截断逻辑
  - [x] 遵循现有代码风格，使用 tweepy 库

- [x] Task 3: 集成到飞书推送系统
  - [x] 在 `app/utils/feishu_notifier.py` 中添加 `_x_feishu_notifier` 全局变量
  - [x] 实现 `init_x_feishu_notifier()` 初始化函数
  - [x] 实现 `get_x_feishu_notifier()` 获取函数
  - [x] 实现 `x_feishu_notify()` 推送函数，格式化推文内容
  - [x] 在 `init_all_notifiers()` 中添加 X 推送初始化
  - [x] 在 `app/main.py` 的 `init_all_notifiers()` 调用中传入 X 配置

- [x] Task 4: 集成到定时任务调度器
  - [x] 在 `app/scheduler.py` 中导入 X 爬虫
  - [x] 在 `full_crawl()` 函数中添加 X 抓取任务调用和飞书推送

- [x] Task 5: 添加 tweepy 依赖
  - [x] 在 `requirements.txt` 中添加 tweepy 依赖

# Task Dependencies
- Task 2 depends on Task 1
- Task 3 depends on Task 1
- Task 4 depends on Task 2 and Task 3
- Task 5 is independent