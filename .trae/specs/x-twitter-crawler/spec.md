# X 平台推文抓取并推送到飞书 - Spec

## Why
需要通过 X (原Twitter) API 抓取指定用户的推文，并自动推送到飞书机器人。需求中特别强调了成本控制：
- 避免重复抓取导致重复扣费（通过永久缓存 since_id 实现增量抓取）
- 移除不必要的 expansions 以节省费用
- 添加月度额度限制，确保不超出预算

## What Changes
- **新增** X (Twitter) 平台推文爬虫实现，遵循现有项目爬虫架构模式
- **新增** 配置项：X API 认证信息、飞书推送配置、抓取额度限制等环境变量
- **新增** 在飞书通知系统中初始化 X 推文飞书推送实例
- **新增** 在定时任务调度器中集成 X 推文抓取任务
- **新增** 持久化状态文件存储：last_tweet_id.json、x_month_count.json、x_day_count.json 用于额度控制和增量抓取
- **遵循** 现有项目架构：X 爬虫放在 `app/crawlers/` 目录，遵循 BaseCrawler 基类模式

## Impact
- Affected specs: 新闻抓取应用，与现有新闻爬虫功能并行，不影响现有功能
- Affected code:
  - `app/config.py` - 添加 X 相关配置项
  - `app/crawlers/x_twitter.py` - 新增 X 爬虫实现
  - `app/crawlers/__init__.py` - 导出 X 爬虫类
  - `app/utils/feishu_notifier.py` - 添加 X 飞书推送初始化和推送函数
  - `app/scheduler.py` - 在 full_crawl 中添加 X 抓取任务调用
  - `app/main.py` - 在 init_all_notifiers 中添加 X 飞书推送初始化
  - `.env.example` - 添加 X 相关环境变量示例

## ADDED Requirements

### Requirement: X 推文增量抓取
系统应能够从 X API 获取指定用户主页推文，仅抓取上次抓取后新增的推文，避免重复扣费。

#### Scenario: 首次抓取
- **WHEN** 系统首次运行
- **THEN** 抓取最新推文（单次不超过 MAX_RESULTS 配置值），更新最大推文 ID 缓存，并推送到飞书

#### Scenario: 非首次抓取无新增
- **WHEN** 系统运行，且无新增推文
- **THEN** API 返回空结果，0 扣费，不推送任何内容

#### Scenario: 非首次抓取有新增
- **WHEN** 系统运行，且存在新增推文
- **THEN** 仅抓取新增推文，更新最大推文 ID 缓存，推送到飞书，仅对新增推文计数扣费

### Requirement: 成本控制
系统应严格控制抓取成本，避免超出 API 月度额度。

#### Scenario: 月度额度检查
- **WHEN** 执行抓取前检查月度累计抓取条数
- **AND** 如果月度累计已达到或超过 MONTH_MAX_LIMIT
- **THEN** 跳过本次抓取，不调用 API，不产生费用

#### Scenario: 当日额度检查
- **WHEN** 执行抓取前检查当日累计抓取条数
- **AND** 如果当日累计已达到或超过 DAY_MAX_LIMIT
- **THEN** 跳过本次抓取，等待明日

#### Scenario: 截断超额抓取
- **WHEN** 本次抓取后当日累计会超过限额
- **THEN** 仅抓取允许数量的最新推文，截断多余数据

#### Scenario: 自动清零
- **WHEN** 进入新的月份/日期
- **THEN** 对应计数器自动清零，重新开始计数

### Requirement: 费用优化
- **WHEN** 请求 X API
- **THEN** 不包含 expansions 用户信息字段，节省每条推文 $0.01 附加费

### Requirement: 飞书推送
系统应将抓取到的推文推送到指定飞书机器人 Webhook。

#### Scenario: 有新增推文
- **WHEN** 成功抓取到新增推文
- **THEN** 格式化推文内容（包含推文 ID、时间、内容），推送到指定飞书 Webhook

#### Scenario: 无新增推文
- **WHEN** 抓取完成但无新增推文
- **THEN** 不发送推送

### Requirement: 数据持久化
系统应将以下状态持久化到文件：
1. 永久保存全局最大推文 ID (`last_tweet_id.json`)，避免重复抓取
2. 保存月度抓取计数 (`x_month_count.json`)，每月 1 号自动清零
3. 保存当日抓取计数 (`x_day_count.json`)，每日 0 点自动清零

### Requirement: 架构一致性
- **WHEN** 实现 X 爬虫
- **THEN** 遵循现有项目架构：继承 `BaseCrawler` 基类，使用现有飞书推送模式 `FeishuNotifier`，集成到现有定时任务调度系统
