# EventDrive

基于 FastAPI 的**多源**财经资讯抓取应用。定时从东方财富、同花顺、新浪财经的公开接口抓取新闻，落库到 SQLite，首页展示当日新闻。

> 抓取链路：**定时任务 → 接口爬虫 → SQLite → Web 页面**。
> 不含浏览器自动化、不含大模型分析、不含飞书推送。

## 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| 后端框架 | FastAPI | 异步友好，自带 OpenAPI 文档 |
| 数据库 | SQLite | 单文件，无需额外安装 |
| 模板引擎 | Jinja2 | FastAPI 自带 |
| 定时任务 | APScheduler | asyncio 调度器 |
| HTTP 客户端 | httpx | 直接调接口，无需浏览器 |
| 图表 | ECharts | 前端可视化 |
| 数据源扩展 | akshare | 宏观/财务/热点指标 |

## 数据来源

### 新闻（3 个源）

全部通过公开 JSON 接口抓取，无需登录、无需 Cookie：

| 源 | 接口 | 返回格式 |
|----|------|----------|
| **东方财富** | `np-listapi.eastmoney.com/comm/web/getNewsByColumns`（财经导读，`column=345`） | 标准 JSON |
| | `newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html`（7x24 快讯补量） | `var ajaxResult={...};` 需剥壳 |
| **同花顺** | `news.10jqka.com.cn/tapp/news/push/stock/`（快讯） | 标准 JSON，需带 Referer |
| **新浪财经** | `feed.mix.sina.com.cn/api/roll/get`（滚动新闻，`pageid=153&lid=2516`） | 标准 JSON |

三源均以 `ctime`（秒级 Unix 时间戳，北京时间）作为发布时间，按 URL 去重、按发布时间倒序。抓到的新闻全部写入 `news` 表。

> 同花顺接口对 Referer 有要求，缺失会被重定向到错误页，爬虫已在请求头中固定携带。

### 其他数据域

| 域 | 数据源 |
|----|--------|
| 市场行情 | Nasdaq API / CBOE / 美国财政部 CSV（FRED 兜底） |
| 指数预警 | `index/*.csv` 离线导入 |
| 宏观指标 | akshare + 央行官网 |
| 股票温度计 | akshare + 交易所官网 |
| 财务指标 | akshare 新浪财报 |
| 今日热点 | akshare 概念板块 |

市场行情页的四个指标：

| 指标 | 代码 | 数据源 |
|------|------|--------|
| 纳斯达克100指数 | `NASDAQ100` | Nasdaq 官方实时报价（备：FRED） |
| VIX恐慌指数 | `VIXCLS` | CBOE 官方历史 CSV |
| 美债2年期收益率 | `DGS2` | 美国财政部每日收益率 CSV（备：FRED） |
| 美债10年期收益率 | `DGS10` | 同上 |

> 这组指标**保持海外数据源不变**。指数与国债类指标各自的备源为 FRED，主源无数据时自动降级。
> 注意：这些均为境外接口，抓取需要服务器能访问外网；`push2his` 类历史 K 线接口在部分国内网络环境下不可达。

## 项目结构

```
EventDrive/
├── app/
│   ├── main.py                 # FastAPI 入口，注册路由与 lifespan
│   ├── config.py               # 环境变量配置
│   ├── database.py             # engine / SessionLocal / Base + schema 兼容
│   ├── models.py               # 11 张表的 ORM
│   ├── schemas.py              # Pydantic 模型
│   ├── crud.py                 # 数据访问层
│   ├── scheduler.py            # APScheduler 编排
│   ├── market_strategy.py      # 峰值回撤策略引擎
│   ├── api/                    # 11 个 router
│   │   ├── news.py             # 新闻列表 / 详情
│   │   ├── crawl.py            # 手动触发抓取 + 抓取状态
│   │   ├── login.py            # 登录鉴权
│   │   ├── market.py           # 市场行情
│   │   ├── index_alarm.py      # 指数预警
│   │   ├── finance.py          # 财务指标
│   │   ├── valuation.py        # 公司估值（DCF）
│   │   ├── macro.py            # 宏观指标
│   │   ├── stock_temp.py       # 股票温度计
│   │   └── hot_sector.py       # 今日热点
│   ├── crawlers/
│   │   ├── base.py             # BaseCrawler / NewsItem
│   │   ├── eastmoney_depth.py  # 东方财富（接口方式）
│   │   ├── ths.py              # 同花顺（接口方式）
│   │   ├── sina.py             # 新浪财经（接口方式）
│   │   └── market_data.py      # 市场行情
│   ├── utils/                  # 14 个工具模块
│   └── templates/              # 14 个页面模板
├── static/                     # CSS / JS / 第三方 echarts
├── data/db.sqlite3             # SQLite 数据库
├── index/*.csv                 # 指数历史数据（FRED 导出）
├── docs/                       # 调度器执行逻辑说明
├── requirements.txt
├── .env.example
├── init_db.py                  # 初始化数据库表
├── run_server.py               # 启动 Web 进程
└── run_scheduler.py            # 启动独立调度器进程
```

## 安装

### 1. 创建虚拟环境

```bash
python -m venv venv

# Windows
.\venv\Scripts\activate
# Ubuntu/macOS
source venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> 新闻抓取已改为纯接口调用，**无需再执行 `playwright install`**。

### 3. 配置环境变量

```bash
# Windows
copy .env.example .env
# Ubuntu/macOS
cp .env.example .env
```

主要配置项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `sqlite:///./data/db.sqlite3` | 数据库路径 |
| `NEWS_PER_SOURCE` | `10` | 每个源保留的最大条数 |
| `NEWS_TIME_RANGE_HOURS` | `24` | 只保留最近 N 小时的新闻 |
| `MIN_DELAY` / `MAX_DELAY` | `2` / `5` | 随机延时区间（秒） |
| `RATE_LIMIT_MIN_INTERVAL` | `3.0` | 同 host 最小请求间隔（秒） |
| `RATE_LIMIT_BURST` | `3` | 令牌桶突发容量 |
| `RATE_LIMIT_MAX_REQS_PER_RUN` | `12` | 单次任务单 host 请求上限 |
| `RATE_LIMIT_DAILY_QUOTA` | `200` | 单 host 每日请求上限（0=不限） |
| `TASK_DAILY_QUOTA` | `8` | akshare 类任务每日执行上限 |
| `START_SCHEDULER` | `false` | 是否在 Web 进程内嵌定时任务 |
| `RELOAD_INDEX_DATA` | `false` | 置 1 强制重导 `index/*.csv` |
| `MACRO_CACHE_TTL_HOURS` | `12` | 页面惰性抓取的缓存有效期 |

### 4. 初始化数据库

```bash
python init_db.py
```

## 运行

### 方式一：Web 与调度器分进程（推荐）

```bash
# 终端 1：Web 服务
python run_server.py
# 或 uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2：定时任务
python run_scheduler.py
```

Windows 下也可直接用 `start.bat` / `start.ps1`。

### 方式二：单进程内嵌调度器

在 `.env` 中设置 `START_SCHEDULER=true`，然后只启动 Web 进程即可，定时任务会一并运行。

> **云端部署自动走此模式**：`run_server.py` 检测到平台注入的 `PORT` 环境变量时，
> 会自动把 `START_SCHEDULER` 置为 `true`（不覆盖显式设置）。因为云沙箱只能运行
> **一个**服务进程，无法再单独拉起 `run_scheduler.py`，定时任务必须内嵌在 Web 进程里。
> 可通过 `/health` 返回的 `scheduler_running` 字段确认调度器是否已启动。

启动后访问 <http://localhost:8000>。

## 定时任务

每日两轮（**北京时间**），当天数据以收盘轮结果为准：

| 轮次 | 时刻 | 抓取内容 |
|------|------|---------|
| **盘前轮** `morning` | 10:00 | 新闻（三源）、今日热点 |
| **收盘轮** `close` | 16:00 | **全部页面**：新闻、财务指标、市场行情、宏观指标、股票温度计、今日热点 |

A 股 15:00 收盘，16:00 抓取即为当日最终收盘值。

### 覆盖语义

同一天内，收盘轮会自然覆盖盘前轮的同日记录，**历史数据不受影响**：

| 数据 | 唯一键 | 覆盖行为 |
|------|--------|---------|
| 新闻 | `url` | **累积去重**：只补充盘前之后新发生的新闻，不删除已有 |
| 市场行情 | `symbol` + `date` | 当天记录被收盘值替换 |
| 今日热点 | `trade_date` + `board_code` | 当天板块被收盘值替换 |
| 宏观 / 股票温度 | 按 `key` 更新最新值 | 历史表按日期追加，趋势曲线完整 |

### 抓取日志

每轮每个数据源都会写入 `crawl_logs` 表（`task` / `session` / `source` / `item_count` / `status` / `duration`），可追溯盘前与收盘两轮的结果差异。

### 其他说明

- 历史数据的回溯补齐（backfill）**不在定时任务内**，需在页面手动触发。
- 财务指标只更新**已入库过**的股票（无法全市场抓取）；新股票需在「抓取控制」页手动输入代码抓取一次。
- 调度器为进程内调度，沙箱重启后重新计时，错过的时刻不会补跑。

## 抓取规则

- **数量限制**：每个源最多保留 `NEWS_PER_SOURCE` 条（默认 10）
- **时间范围**：抓取时仅保留最近 `NEWS_TIME_RANGE_HOURS` 小时的新闻（默认 24）
- **去重**：按原文 URL 去重（`news.url` 有 UNIQUE 约束），已存在的新闻不重复写入
- **排序**：按发布时间倒序
- **落库**：三源抓到的新闻统一写入 `news` 表，页面与 API 均从库中读取
- **抓取量**：每源单轮只请求 1 页/1 个接口，抓取量收敛到「保留条数 + 少量冗余」（约 20 条），不做多页连翻

## 抓取限流（防频繁 / 防超量）

所有外部数据源请求统一受 `app/utils/anti_crawl.py` 的 `RateLimiter` 约束，**按 host（域名）独立计量**：

| 机制 | 默认值 | 作用 |
|------|--------|------|
| 最小请求间隔 | 3.0s | 同一 host 两次请求的强制间隔（令牌桶） |
| 突发容量 | 3 | 允许的突发请求数，超出即排队等待 |
| 单次任务请求上限 | 12 | 一次抓取任务内单个 host 最多发几次请求，超出直接放弃 |
| 每日配额 | 200 | 单 host 每天请求次数上限，0 表示不限制 |
| 失败指数退避 | 60s → 封顶 1800s | 连续失败按 `基数 × 2^n` 增长冷却，成功后清零 |
| 任务级每日配额 | 8 | akshare 等内部封装、无法逐请求插桩的来源；页面手动刷新与定时任务共用此额度 |

此外各抓取任务都有**防重入锁**：定时任务与页面手动触发撞在一起时，后到者直接跳过。

配置项（均可通过 `.env` 覆盖，前缀 `RATE_LIMIT_` / `TASK_DAILY_QUOTA`）：

```bash
RATE_LIMIT_MIN_INTERVAL=3.0
RATE_LIMIT_BURST=3
RATE_LIMIT_MAX_REQS_PER_RUN=12
RATE_LIMIT_DAILY_QUOTA=200
RATE_LIMIT_BACKOFF_BASE=60
RATE_LIMIT_BACKOFF_MAX=1800
RATE_LIMIT_SKIP_ON_EXCEED=true
TASK_DAILY_QUOTA=8
```

运行期状态可通过 `get_limiter().snapshot()` 与 `task_quota_snapshot()` 查看（各 host 今日请求数、剩余令牌、连续失败次数、退避剩余秒数）。

> 指数预警页读的是本地 `index/*.csv`，不发外部请求，因此不经过限流。

## 页面一览

| 路径 | 页面 |
|------|------|
| `/` `/home` | 新闻列表（支持搜索、来源筛选、分页） |
| `/news/{news_id}` | 新闻详情 |
| `/crawl` | 抓取控制台（手动触发 + 实时日志） |
| `/market` | 市场行情 |
| `/index-alarm` | 指数预警 |
| `/macro` `/macro-trend` | 宏观指标 |
| `/stock-temp` `/stock-trend` | 股票温度计 |
| `/finance` | 财务指标 |
| `/valuation` | 公司估值 |
| `/hot-sector` | 今日热点 |
| `/login` | 登录 |
| `/health` | 健康检查 |

新闻列表默认**只展示当日新闻**（勾选框「仅看当日」默认开启）。取消勾选则回退为最近 24 小时。

需要查阅导入的历史新闻时，用 `GET /api/news?all_history=true` 显式获取全量历史（不做时间过滤），可叠加 `source` / `include_keywords` 筛选。该参数不影响首页默认行为。

## 已知边界

- 三个新闻源均依赖**非公开**接口，接口路径或字段变更时需同步调整 `app/crawlers/` 下相应文件中的接口常量与字段映射。
- 市场行情依赖 Nasdaq / CBOE / 美国财政部 / FRED 等**境外接口**，抓取需服务器能访问外网；接口变更时需调整 `market_data.py`。
- 接口返回的 `summary` / `digest` / `intro` 已包含正文要点，`fetch_news_detail()` 不再回源抓取详情，因此 `content` 字段存的是摘要而非全文。
- 图片 URL 仅在内存链路中传递（`NewsItem.image_url`），未下载到本地，`image_path` 恒为 `NULL`。

## 温度计与趋势页的口径

### 综合温度权重

市场温度 = **估值 35% + 情绪 35% + 资金 30%**（层权重集中在 `stock_temp.py` 的 `TEMP_LAYERS`，改这一处即全局生效；层内权重不变）。各层：

| 层 | 组成 |
|---|---|
| 估值 | PE 分位 40% + ERP 30% + 破净率 30% |
| 情绪 | 成交额分位 50% + 换手率分位 30% + 两融占流通市值 20% |
| 资金 | 公募仓位 30% + 股票ETF份额 25% + 新基金发行 25% + 产业资本 20% |

### 趋势页数据从零积累

`/stock-trend` 与 `/macro-trend` **不做历史回溯**，数据由定时任务每天收盘逐日写入历史表，曲线越用越长。两页已删除「补齐历史数据」按钮，`POST /api/*/backfill` 仅保留为手动补数的备用接口（页面无入口）。

因此：

- 刚启用时每条曲线只有 1 个点（前端按 1 点画圆点、≥2 点画线），属预期行为。
- **月频与季频指标攒得慢**：宏观的 PMI / PPI / 社融 / M1-M2 是月度，超储率是季度。
- **两融曲线画的是「占流通市值%」**（与指标卡判档同口径，阈值 2.0 / 2.5 / 3.0 / 3.5%），不是余额。逐日流通市值没有批量数据源，所以该序列同样是每天一个点。
- 宏观趋势页只保留各指标曲线，跨指标合成图（四象限、36 月 z-score）已移除 —— 它们依赖长期月度历史，从零积累会长期空白。

### 宏观温度计

- 总分 = 资金面 25% + 实体热度 45% + 信用/部门 30%，打分锚点见 `macro_thermometer.py` 的 `SUB_INDICES`（**一经确定不建议改，否则历史不可比**）。
- 分项表含**数据日期**列，日度显示完整年月日、月度/季度显示 `YYYY-MM`。各分项频率不同（DR007 日度、超储率季度、PMI 等月度），总分是不同时间尺度读数的加权，日期列用于避免误读。
- 缺口径处理：某分项缺数据时按「可用项权重归一化」并在表下列出缺失项，不会让整根温度计作废。

## 前端资源与缓存

- 所有图表库**本地化**：`static/js/echarts.min.js`（5 个页面共用）、`static/js/chart.umd.min.js`（市场行情页）。不使用外部 CDN。
- 静态资源统一带 `Cache-Control: no-store` 与 `?v=<启动时间戳>`（由 `jinja_env.globals["static_ver"]` 注入），服务重启即换 URL，避免改了样式却因浏览器缓存看不到变化。

## 许可证

MIT License