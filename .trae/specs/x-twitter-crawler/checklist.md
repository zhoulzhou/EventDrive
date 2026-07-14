# Checklist

- [x] `app/config.py` 包含所有 X 相关配置项（CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET, X_FEISHU_WEBHOOK_URL, X_FEISHU_KEYWORD, X_MAX_RESULTS, X_MONTH_MAX_LIMIT, X_DAY_MAX_LIMIT）
- [x] `.env` 或 `.env.example` 包含 X 相关环境变量示例
- [x] `app/crawlers/x_twitter.py` 存在且实现完整（持久化状态管理、额度检查、增量抓取、截断逻辑）
- [x] X 爬虫使用 tweepy 库，不包含 expansions 参数
- [x] 持久化文件（last_tweet_id.json、x_month_count.json、x_day_count.json）读写逻辑正确
- [x] 月度/当日额度检查逻辑正确，含自动清零
- [x] `app/utils/feishu_notifier.py` 包含 X 飞书推送初始化、获取和推送函数
- [x] `init_all_notifiers()` 包含 X 推送初始化参数
- [x] `app/main.py` 中 `init_all_notifiers()` 调用传入 X 配置
- [x] `app/scheduler.py` 中 `full_crawl()` 包含 X 抓取和推送任务
- [x] `requirements.txt` 包含 tweepy 依赖
- [x] 代码不破坏现有功能，所有现有爬虫和飞书推送正常工作