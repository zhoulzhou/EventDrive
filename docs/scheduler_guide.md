# Scheduler 执行逻辑

> 本文档描述 `app/scheduler.py` 的当前实现。
> 原多数据源串行执行、飞书推送、大模型分析相关章节已随功能移除而删除。

## 抓取限流（全局约束）

所有外部数据源请求统一经过 `app/utils/anti_crawl.py` 的 `RateLimiter`，**按 host 独立计量**：

| 机制 | 默认值 | 说明 |
|------|--------|------|
| 最小请求间隔 | 3.0s | 同一 host 两次请求的强制间隔（令牌桶） |
| 突发容量 | 3 | 超出即排队等待 |
| 单次任务请求上限 | 12 | 一次任务内单 host 最多发几次请求，超出放弃 |
| 每日配额 | 200 | 单 host 每天请求次数上限 |
| 失败指数退避 | 60s 起，封顶 1800s | 连续失败 `基数 × 2^n`，成功后清零 |
| 任务级每日配额 | 8 | akshare 类来源（内部封装，无法逐请求插桩） |

**任务级防重入**：`check_task_allowed()` 保证同一任务不会并发执行；定时任务与页面手动触发撞车时，后到者直接跳过。

涉及的任务：`news_crawl`、`market`、`macro`、`stock_temp`、`hot_sector`。

运行期观察：`get_limiter().snapshot()`、`task_quota_snapshot()`。

指数预警页读本地 `index/*.csv`，不发外部请求，不经限流。

## 定时任务总览

每日两轮，统一用**北京时间**（`CN_TZ`），当天数据以收盘轮为准：

| 任务函数 | 时刻 | 说明 |
|----------|------|------|
| `morning_session` | 10:00 | 盘前轮：新闻（`full_crawl`）+ 今日热点（`refresh_hot_sector`） |
| `close_session` | 16:00 | 收盘轮：全部页面数据 |

`close_session` 内部顺序：

```
1. full_crawl              → 新闻（三源并发）
2. refresh_financials      → 财务（串行，仅更新已入库股票）
3. asyncio.gather 并发：
   ├─ crawl_market_data       市场行情
   ├─ refresh_macro_indicators 宏观指标
   ├─ refresh_stock_temp      股票温度计
   └─ refresh_hot_sector      今日热点
```

### 覆盖语义

同一天内收盘轮自然覆盖盘前轮的同日记录，历史累积不受影响：

| 数据 | 唯一键 | 覆盖行为 |
|------|--------|---------|
| 新闻 | `url` | 累积去重（只补充新发生的） |
| 市场行情 | `symbol` + `date` | 当天替换为收盘值 |
| 今日热点 | `trade_date` + `board_code` | 当天替换为收盘值 |
| 宏观 / 股票温度 | `key` | 更新最新值，历史表按日期追加 |

### 抓取日志

每轮每个数据源调用 `_write_crawl_log()` 写入 `crawl_logs` 表：

| 字段 | 含义 |
|------|------|
| `task` | news / finance / market / macro / stock_temp / hot_sector |
| `session` | morning / close / manual |
| `source` | 数据源名称或股票代码 |
| `item_count` | 本次抓取条数 |
| `status` | ok / empty / failed / skipped |
| `duration` | 耗时（秒） |

写日志内部吞异常，失败不影响抓取主流程。

## 新闻抓取（full_crawl）

### 数据源

三个源并发抓取，均为接口调用：

| 源 | Crawler 类 | 接口 |
|----|-------------|------|
| 东方财富 | `EastmoneyDepthCrawler` | np-listapi 财经导读 + newsapi 7x24 快讯 |
| 同花顺 | `THSNewsCrawler` | `news.10jqka.com.cn/tapp/news/push/stock/` |
| 新浪财经 | `SinaNewsCrawler` | `feed.mix.sina.com.cn/api/roll/get` |

### 执行流程

```
1. news_sources = [EastmoneyDepthCrawler, THSNewsCrawler, SinaNewsCrawler]
2. asyncio.gather 并发执行（信号量并发数 CONCURRENT_CRAWLERS = 2）
   └─ crawl_single_source(<Crawler>)
        ├─ crawler.crawl()
        │    ├─ fetch_news_list()          # 接口调用，放入线程池避免阻塞事件循环
        │    │    └─ 按 URL 去重 → 按 publish_time 倒序
        │    ├─ 截取至 NEWS_PER_SOURCE 条
        │    └─ 过滤超出 NEWS_TIME_RANGE_HOURS 的条目
        └─ 逐条落库
             ├─ crud.is_news_exists(db, url)   # 按 URL 判重
             └─ crud.create_news(db, ...)      # 不存在才写入并 commit
3. 逐源打印结果：<源名> 抓取到 N 条新新闻 / 没有新新闻
4. 汇总：保存 N 条, 耗时 X 秒
```

### 落库判重

以原文 URL 为唯一依据（`news.url` 有 UNIQUE 约束）：

- `crud.is_news_exists(db, url)` 返回 True → 跳过，记「已存在，跳过」
- 返回 False → 写入 `news` 表并计入 `total_saved`

三个源共用同一套入库逻辑，差异仅在爬虫实现。

### 各源要点

| 源 | 要点 |
|----|------|
| 东方财富 | 快讯接口返回 `var ajaxResult={...};`，需用正则剥壳后 `json.loads`；深度新闻不足时用快讯补齐 |
| 同花顺 | **必须携带 `Referer: https://news.10jqka.com.cn/`**，缺失会被 302 到错误页；分页参数 `page` / `limit`；`ctime` 为秒级时间戳 |
| 新浪财经 | 分页参数 `pageid=153` / `lid=2516` / `num` / `page`；摘要取 `intro` 字段；`ctime` 为秒级时间戳 |

### 进度回调

`full_crawl` 通过 `log_crawl()` 输出进度，由 `set_crawl_progress_callback()` 注册的回调转发到 `/api/crawl/status`，供页面「抓取控制」实时展示。

## 市场行情（crawl_market_data）

### 指标与数据源

四个指标**保持海外数据源不变**：

| 指标 | 代码 | 主源 | 备用源 |
|------|------|------|--------|
| 纳斯达克100指数 | `NASDAQ100` | Nasdaq 官方实时报价 `api.nasdaq.com` | FRED（NASDAQ100） |
| VIX恐慌指数 | `VIXCLS` | CBOE 官方历史 CSV | — |
| 美债2年期收益率 | `DGS2` | 美国财政部每日收益率 CSV | FRED（DGS2） |
| 美债10年期收益率 | `DGS10` | 美国财政部每日收益率 CSV | FRED（DGS10） |

> 这组指标按用户要求**不替换为国内源**。均为境外接口，抓取需服务器能访问外网。

### 流程

1. 调度器触发 `crawl_market_data()`
2. `refresh_market_data()` 判断美股是否交易日（`_is_us_market_open` 排除周末与美国主要节假日，含复活节算法推算耶稣受难日）
3. 按指标的 `source` 分发：nasdaq / cboe / treasury；主源无有效值时回退 FRED
4. 写入 `market_prices` 表（`symbol` + `date` 唯一约束），数据未变化时跳过重复落库
5. 落库后调用 `advance()` 推进峰值回撤策略状态
6. 页面 `/api/market` 从数据库读取；库为空时兜底实时抓取一次

### 分级策略

| 指标 | 策略 | 配置位置 |
|------|------|---------|
| `NASDAQ100` | 峰值回撤：<5% 绿 / 5~10% 黄 / ≥10% 红；含 `fixed_peak_value` 固定峰值播种 | `data/market_strategy.json` |
| `VIXCLS` | 按绝对值分级：<20 绿 / 20~30 黄 / ≥30 红 | 同上，`thresholds` |

## 宏观指标（refresh_macro_indicators）

- 抓取宏观指标「当前读数」（资金面 / 经济热度），顺带把读数变化记入历史表
- 抓取是同步阻塞的（akshare + 央行官网），用 `asyncio.to_thread` 执行避免卡住事件循环
- 写入 `macro_indicators` 与 `macro_indicator_history`
- 与页面接口 `/api/macro/refresh` 共用 `app/utils/macro_refresh.py` 的实现
- 长历史序列由页面「补齐历史数据」按钮触发（`POST /api/*/backfill`），不在定时任务内回溯

## 股票温度计（refresh_stock_temp）

- 抓取 A 股冷热三层指标快照，写入 `stock_temp_indicators` 与 `stock_temp_history`
- 数据源：akshare + 交易所官网 + 东财数据中心
- 与页面接口 `/api/stock-temp/refresh` 共用 `app/utils/stock_temp_refresh.py`
- 在 16:00 收盘轮触发，只抓当前读数

## 今日热点（refresh_hot_sector）

- 抓取概念板块热点前三，写入 `hot_sector_snapshots`
- 数据源：akshare（阻塞调用已在线程池内执行），此处直接 await
- 写入时机只有两个：用户点页面「刷新盘面」，或本定时任务
- 同一交易日同一板块就地覆盖，取当日最终结果
- 页面打开时只读库展示，不发外部请求

## 历史数据策略：只累积，不回溯

两个趋势页（`/stock-trend`、`/macro-trend`）的数据**从零开始逐日积累**，不做历史回溯：

- 定时任务每次抓取都会把读数写进历史表（`stock_temp_history` / `macro_indicator_history`），曲线随天数自然变长。
- 页面已删除「补齐历史数据」按钮，两个 backfill 接口仅作为**手动补数的备用入口**保留，正常使用不需要也不建议调用：

  | 接口 | 作用 |
  |------|------|
  | `POST /api/macro/backfill` | 补齐宏观指标历史序列 |
  | `POST /api/stock-temp/backfill` | 补齐股票指标历史序列 |

- 调用回填时注意：交易所日频数据需逐日请求（2 接口 × N 天），`exchange_days=60` 约需数分钟到数十分钟；`push2his.eastmoney.com` 类历史接口在部分网络环境不可达。

指数数据（`index/*.csv`）为一次性导入：应用启动时若 `index_history` 表为空则导入，置环境变量 `RELOAD_INDEX_DATA=1` 可强制清空重导。

**刚启用时的预期现象**：每条曲线只有 1 个点（前端按 1 点显示圆点、≥2 点才连线）；日度指标次日成线，月度指标（PMI / PPI / 社融 / M1-M2）需等下个月，季度指标（超储率）需等下个季。这不是故障。

## 启动方式

### 分进程（推荐）

```bash
python run_server.py    # Web 进程
python run_scheduler.py # 调度器进程
```

### 单进程内嵌

`.env` 中设置 `START_SCHEDULER=true`，Web 进程启动时一并拉起调度器（`main.py` 的 lifespan）。

## 关键文件

| 文件 | 职责 |
|------|------|
| `app/scheduler.py` | 全部任务编排与调度注册 |
| `app/crawlers/eastmoney_depth.py` | 东财接口爬虫 |
| `app/crawlers/ths.py` | 同花顺接口爬虫 |
| `app/crawlers/sina.py` | 新浪财经接口爬虫 |
| `app/crawlers/market_data.py` | 市场行情抓取 |
| `app/utils/macro_refresh.py` | 宏观指标抓取 |
| `app/utils/stock_temp_refresh.py` | 股票指标抓取 |
| `app/utils/hot_sector_refresh.py` | 今日热点抓取 |
| `app/api/crawl.py` | 手动触发与状态查询 |
| `run_scheduler.py` | 独立调度器进程入口 |

## 常见问题排查

### 新闻抓取不到数据

1. 逐源检查接口可达性：`np-listapi.eastmoney.com`、`newsapi.eastmoney.com`、`news.10jqka.com.cn`、`feed.mix.sina.com.cn`
2. **检查是否被限流拦截**：日志中「触发限流，跳过本轮抓取」说明令牌不足 / 配额耗尽 / 处于失败退避中；
   用 `get_limiter().snapshot()` 查看各 host 的「今日请求 / 剩余令牌 / 退避剩余」
3. 同花顺若返回错误页，多半是 Referer 缺失
4. 确认返回条数是否被 `NEWS_PER_SOURCE` 与 `NEWS_TIME_RANGE_HOURS` 过滤掉
5. 三个源均为非公开接口，字段或路径变更时需调整对应爬虫文件

### 抓取任务被跳过

1. 日志「正在运行中，跳过本次触发（防重入）」= 上一次尚未结束，等它跑完即可
2. 日志「今日已执行 N/8 次，达到配额上限」= `TASK_DAILY_QUOTA` 用尽，
   需等待次日或调大该配置（注意这也意味着你确实在频繁触发）
3. 接口连续失败会触发指数退避（最长 30 分钟），期间该来源的所有抓取都会被拒

### 首页没有新闻

1. 首页默认只展示**当日**新闻，若库中最新数据是昨天的，页面会为空
2. 取消页面「仅看当日」勾选，改为查看最近 24 小时
3. 检查 `news` 表是否真有数据：`SELECT source, COUNT(*), MAX(publish_time) FROM news GROUP BY source;`
4. 确认抓取任务已成功执行（看 `crawl_logs` 或 `/api/crawl/status`）

### 抓取任务不触发

1. 确认调度器进程是否在运行（`run_scheduler.py` 或 `START_SCHEDULER=true`）
2. 所有定时任务统一用**北京时间**（`CN_TZ`）：10:00 盘前轮 / 16:00 收盘轮
3. 查看启动日志中「已注册任务」列表与 `next_run_time`（应为 `morning_session_job` 与 `close_session_job` 两条）
4. 查看 `crawl_logs` 表确认最近一次抓取结果与状态

### 市场行情获取失败

1. 检查 `api.nasdaq.com`、`cdn.cboe.com`、`home.treasury.gov`、`fred.stlouisfed.org` 是否可访问（均为境外接口）
2. 主源失败会自动降级到 FRED，注意观察日志中的降级提示
3. 非美股交易日（周末、美国节假日）会正常跳过，属预期行为
4. 确认 `market_prices` 表是否有数据
5. 若服务器无外网，该页数据将无法更新（此时可考虑改用国内数据源）

### 页面指标显示为空

1. 该指标可能未配置策略配置（`data/market_strategy.json`），检查 JSON 格式是否正确
2. 首次部署若库为空，页面接口会兜底实时抓取一次，检查网络是否可达

### 数据重复写入

去重依据是 `news.url`。若出现重复，检查是否 URL 字段异常（如空值），以及 `crud.is_news_exists` 的查询逻辑。