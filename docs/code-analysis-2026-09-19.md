# EventDrive 代码结构分析与改进方案

> 分析日期：2026-09-19 ｜ 范围：仓库 `D:\AppData\EventDrive` 全部自有代码（排除 `.venv/`、`static/js/echarts.min.js`）
> 本文只做分析与方案设计，**未修改任何代码**。所有结论均标注了文件:行号，可直接核对。
> 唯一新增文件即本文档。

---

## 一、总体判断

项目实际是一个「多域个人数据中台」，不是 README 里描述的「5 大网站新闻抓取应用」。真实能力已经覆盖 5 个业务域：

| 域 | 入口页面 | 数据表 | 数据源 | 状态 |
|---|---|---|---|---|
| 新闻聚合 | `/home` | `news` | 东方财富(Playwright)、NYT(API)、BBC(RSS) | **实测 0 条数据，主功能不可用** |
| 市场行情 | `/market` | `market_prices`、`market_strategy_state` | Nasdaq API / CBOE / 美财政部 CSV（FRED 兜底） | 可用（3 个交易日 11 行） |
| 指数预警 | `/index-alarm` | `index_history` | `index/*.csv` 一次性导入 | 可用（2967 行） |
| 财务指标 | `/finance` | `financial_reports` | akshare 新浪财报 | 可用但 0 条数据 |
| 公司估值 | `/valuation` | `company_valuations` | 前端输入 + DCF | 可用但 0 条数据 |

代码规模（不含 `.venv`）：自有 Python 约 4,400 行、模板 HTML 约 2,400 行、CSS/JS 约 1,200 行；另有 **46 个根目录零散脚本**（约 1,900 行）不属于应用本体。

**核心结论**：模块划分（api / crawlers / utils / models / crud / schemas）方向正确，但存在 5 类系统性问题——
① **安全边界失效**（无鉴权写接口 + 硬编码密钥 + 密钥已进入 git 远程历史）；
② **文档与代码严重脱节**（README/需求文档描述的是另一个项目）；
③ **抽象被架空**（`BaseCrawler` 的模板方法被 4 个子类全部绕过，配置项形同虚设）；
④ **复制粘贴主导**（模板、脚本、分析器、通知器大量重复）；
⑤ **零测试、零静态检查、无迁移机制**，且多处在 import 期执行副作用。

---

## 二、项目实际拓扑

```
EventDrive/
├── app/                        # ← 真正的应用（唯一应保留的代码主体）
│   ├── main.py                 # FastAPI 入口（183 行）+ 10 个页面路由
│   ├── config.py               # 手写 os.getenv 配置（90 行，无校验）
│   ├── database.py             # engine/SessionLocal/Base + 手写「迁移」函数
│   ├── models.py               # 6 张表的 ORM（93 行）
│   ├── schemas.py              # Pydantic（78 行，已与实际模型脱节）
│   ├── crud.py                 # 全部数据访问（420 行，唯一"胖"模块）
│   ├── scheduler.py            # APScheduler 编排（290 行）
│   ├── market_strategy.py      # 峰值回撤策略引擎（284 行，设计良好）
│   ├── api/                    # 10 个 router：news/crawl/backup/feishu/login/market/index_alarm/finance/valuation
│   ├── crawlers/               # base + eastmoney(Playwright) + nytimes + bbc + market_data + x_twitter
│   ├── utils/                  # feishu_notifier(612) + 3 个 LLM 分析器 + finance_indicators + anti_crawl + local_backup
│   └── templates/              # base + 8 个页面（内含约 630 行内联 CSS）
├── static/                     # style.css(638) + main.js(55) + vendored echarts(1MB)
├── data/                       # db.sqlite3(548KB, 活跃) + market_strategy.json + 2 个 0 字节废弃库
├── index/                      # 4 个 FRED CSV + chart_2015.html(355KB 生成物)
├── backup/                     # 1 个与活跃库完全同内容的 sqlite 快照
├── docs/, .trae/specs/         # 文档与 AI 规格笔记
└── <根目录 46 个脚本>            # 14 个 sys.path hack、11 个重复启动器、6 个调试残留
```

调用链（新闻侧）：
`main.py(lifespan/import) → scheduler.full_crawl() → crawlers/*.crawl() → crud.create_news → news 表`
`→ feishu_notifier 队列 → LLM 分析器 → 飞书 webhook`

调用链（行情侧）：
`scheduler.crawl_market_data() → market_data.refresh_market_data() → save_market_prices() → market_strategy.advance() → market_strategy_state`
`→ /api/market 读库 → compute_drawdown / compute_level → 前端着色`

---

## 三、P0 级问题（必须先修）

### 3.1 `POST /api/crawl/trigger` 完全无鉴权 — 可被任意人触发的 DoS/资源消耗

`app/api/crawl.py:34-35` 是唯一一个没有 `Depends(require_auth)` 的写接口（同文件 `:76-77` 的 `/crawl/status` 有鉴权）：

```python
@router.post("/crawl/trigger")
async def trigger_crawl():          # ← 无 auth 参数
```

该接口会拉起 Playwright 无头 Chromium、调用 3 个大模型 API、并向 10 个飞书群推送。任何能访问该端口的人都可以无限触发。同类问题：`POST /api/logout`（`app/api/login.py:61-62`）亦无鉴权。

### 3.2 `data/db.sqlite3` 的新闻表为 0 行 — 核心需求未达成

实测：`news` 表 **0 行**，`financial_reports` 0 行，`company_valuations` 0 行；`market_prices` 仅 11 行 / 3 个日期。`em_test.log` 记录了东方财富抓取的真实失败堆栈：

```
playwright._impl._errors.TimeoutError: Page.goto: Timeout 60000ms exceeded.
 - navigating to "https://finance.eastmoney.com/a/ccjdd.html", waiting until "networkidle"
```

即：项目第一验收标准「能成功从 N 个网站抓取新闻」在真实环境中从未成功（至少东方财富侧）。改进方案见 §7 第 1 项。

### 3.3 `cleanup_sparse_market_dates` 的 SQL 逻辑错误会**删除全部近期行情数据**

`app/crud.py:143`：

```python
.having(func.count(func.nullif(models.MarketPrice.value, None)) < 3)
```

`NULLIF(value, NULL)` 在 SQLite 中恒为 NULL，`COUNT(NULL)` 恒为 0，因此 `HAVING 0 < 3` **对每一天都成立**。实测编译结果：

```
count(nullif(market_prices.value, NULL))
```

后果：用户点一次「清洗数据」（`market.html:365` → `/api/market/cleanup`），会删除最近 30 个日期内**所有** symbol 的全部记录，而不是只删稀疏日。当前库只有 11 行、3 个日期，**触发一次即清空全部行情历史**。

### 3.4 登录体系不可用于生产

`app/api/login.py`：

| 行 | 问题 |
|---|---|
| `:8` | `SECRET_KEY = "kaiamu_secure_secret_key_2024"` 硬编码在源码并已提交 git → 任何人可离线伪造会话 cookie |
| `:12-14` | 用户名 `123123` 与密码哈希硬编码，无法改密、无多用户 |
| `:51-57` | `set_cookie` 缺 `secure=True`，无 CSRF token，`max_age=7天` 且无服务端吊销 |
| 全局 | 用 `itsdangerous.URLSafeSerializer` 而非 `TimedJSONWebSignatureSerializer`/`SessionMiddleware`，token 无过期时间字段 |

`is_logged_in` 只校验签名有效性，不校验用户名是否仍在允许列表，也不校验签发时间。

### 3.5 真实密钥已进入远程 git 历史 — 需立即轮换

```
$ git log --all --format='%h %s' -- .env
75d719e  临时上传 .env 配置文件（后续删除）      ← 首次提交
...（共 10 个提交包含 .env：含 OPENROUTER_API_KEY、DEEPSEEK_API_KEY、
     KB_API_KEY、NYT_API_KEY、X_B_T、X_CONSUMER_SECRET、X_ACCESS_TOKEN、
     以及多个飞书 webhook 真实 token）
e9a5a60  chore: 添加.env到.gitignore并从版本控制移除   ← 仅"停止跟踪"，历史仍在

$ git branch -r --contains 337972c
  origin/master          ← 已推送到 GitHub，历史中的密钥永久可获取
```

`.gitignore:30` 已忽略 `.env`，**但 git 历史不可撤销**。处置方案见 §7 第 0 项。

### 3.6 全部 4 个爬虫都覆盖了 `BaseCrawler.crawl()`，模板方法被彻底架空

`app/crawlers/base.py:50-89` 定义了两个关键策略：`settings.NEWS_PER_SOURCE` 上限（`:62,80`）与 `is_within_time_range` 24 小时过滤（`:72`）。但：

| 子类 | 覆盖位置 | 上限 | 时间过滤 |
|---|---|---|---|
| `EastmoneyDepthCrawler` | `eastmoney_depth.py:161` | 硬编码 10（`:171,178`） | **显式 return True**（`:155-156`） |
| `NYTDepthCrawler` | `nytimes.py:410` | 硬编码 12（`:425`） | 无 |
| `BBCCrawler` | `bbc.py:121` | 硬编码 6（`:135`） | 无 |
| `NYTCrawler` | `nytimes.py:169` | 硬编码（`fetch_news_list` 内切片） | 无 |

后果：`settings.NEWS_TIME_RANGE_HOURS` 与 `settings.NEWS_PER_SOURCE` **在任何抓取路径上都从未生效**；`.env` 里配 `NEWS_PER_SOURCE=10` 是误导。同时 4 个 `crawl()` 各自调用已废弃的 `asyncio.get_event_loop()`（Python 3.12+ 在无运行循环时告警/报错）。

**这个覆盖还埋了一个"改回去就炸"的陷阱**：`nytimes.py:147-158` 的 `_parse_display_time` 在带偏移分支返回 **aware** datetime，而基类 `base.py:47-48` 用 naive `datetime.now()` 与之比较。一旦按 §7-6 的建议删掉子类覆盖、回归 `base.crawl()`，NYT 会立刻抛 `TypeError: can't compare offset-naive and offset-aware datetimes`。修复顺序必须是：**先统一时间解析（全部 naive-UTC），再删除覆盖**。

同文件还有 4 处死代码：`_parse_display_time` 两份逐字重复（`:140`、`:381`）、`_parse_publish_time` 两份重复（`:102`、`:343`，且转 UTC naive，与前者语义矛盾）、`fetch_news_detail`（`:56`、`:289`）从不被调用（故 `content` 永远只有 abstract）、`NYTCrawler` 仅在 `crawlers/__init__.py:3,10` 导出而**无任何实例化点**。此外 `NYTDepthCrawler` 合并 wire + topstories 时**不按 url 去重**（`:206-211`），两次 API 调用之间零延迟、无 429 退避；7 处 `except Exception` 只写 `self.error_message`（`:51/70/246/284/303/339/428`），而**全仓没有任何代码读取该属性**——`NYT_API_KEY` 为空导致的 401 被彻底吞掉，最终只表现为 `scheduler.py:238` 的「纽约时报没有新新闻」。

### 3.7 新闻列表分页在前端根本无法工作

`/api/news`（`app/api/news.py:12`）返回**裸数组** `List[schemas.News]`，没有 `total`。前端 `index.html:44-49`：

```javascript
const newsList = Array.isArray(data) ? data : data.news || [];
const total = Array.isArray(data) ? data.length : data.total || newsList.length;
```

`total` 恒等于本页条数（≤10），`Math.ceil(total/10)` 恒为 1 → `renderPagination` 永远隐藏分页控件。翻页功能实际不可用。此外 `crud.get_news_list`（`crud.py:21-58`）没有 `count` 查询，后端也没有可用于分页的总数接口。

### 3.8 前端 XSS：抓取日志未转义直插 innerHTML

`app/templates/crawl_control.html:260-263`：

```javascript
container.innerHTML = logs.map(log => {
    const logClass = getLogClass(log.message);
    return `<div class="log-item">...<span class="${logClass}">${log.message}</span></div>`;
}).join('');
```

`log.message` 的内容来自 `app/scheduler.py:85`：

```python
log_crawl(f"[{source_name}] 处理第 {idx+1}/{len(news_items)} 条: {news_item.title[:30]}...")
```

即**抓取到的第三方网页标题会被原样插入 innerHTML**，未做任何转义。恶意/被篡改的新闻标题（含 `<img onerror=...>`）可在管理员打开抓取控制页时执行任意 JS——而该页 cookie 正是管理员会话。

同类未转义点（已核实）：`index.html:65` `${item.source}`、`index.html:63` `item.id` 拼进内联 `onclick`、`news_detail.html:31/43/51`、`market.html:83/87/89/92/119-121`、`finance.html:172`。另外 `main.js:29-33` 的 `escapeHtml` 基于 `textContent→innerHTML`，**不转义双引号**，却被用在属性位置（`finance.html:42` 的 `value="..."`、`news_detail.html:51` 的 `alt="..."`），仍可被 `"` 突破。

### 3.9 Jinja 环境未开启 autoescape

`app/main.py:93`：

```python
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))
```

未传 `autoescape=True`，也未使用 FastAPI 的 `Jinja2Templates`。当前唯一服务端变量是 int 类型的 `news_id`（`main.py:148`），暂无实际注入点，但这是"零安全网"状态——一旦有字符串变量传入即变 XSS。同时 `render_template`（`:110-114`）手写渲染，绕过了 Starlette 的模板异常处理，模板报错只会变成 500 且无定位信息。

---

## 四、P1 级问题（影响正确性与可维护性）

### 4.1 数据模型与真实库结构已漂移

**（a）`news` 表缺 `news_type` 列。** `需求文档.md:36` 要求该字段，`app/crawlers/nytimes.py` 为每条新闻生成 `news_type`（wire/topstories），`app/scheduler.py:98` 也用 `getattr(news_item, 'news_type', None)` 传递——但 `app/models.py:7-20` 的 `News` 没有这一列。实测 `PRAGMA table_info(news)` 确认不存在。NYT 区分「最新资讯/精选」的核心需求因此无法落库。

**（b）库中 3 张孤儿表。** 实测存在 `crawl_logs`、`filter_rules`、`index_highs`，但代码中已无对应模型（日志功能在 `bf83e36` 提交被移除，筛选规则功能也已消失）。它们只增不减地留在生产库中。

**（c）`schemas.MarketPrice` 丢字段。** `app/schemas.py:52` 继承的是 `BaseModel` 而非 `MarketPriceBase`，实测字段集为 `['id','created_at','updated_at']`——`symbol/name/unit/value/date` 全部丢失。任何以该 schema 作为 `response_model` 的接口都会静默丢数据（当前尚未被使用，属定时炸弹）。

**（d）Schema 迁移靠手写补丁。** `app/database.py:41-65` 的 `ensure_schema_compatibility` 用 `DROP TABLE market_prices` 处理旧结构、用 `ALTER TABLE ... ADD COLUMN` 补 `stock_name`，并且**在 `app/main.py:28` 的 import 期执行**。没有 Alembic，没有版本号，没有回滚路径。

**（e）导入期副作用。** `app/main.py` 在模块级（`:24-63`）打印横幅、建表、导入 2967 行 CSV、初始化全部飞书通知器。任何 `import app.main`（包括 openapi 生成、测试收集、`subprocess_start.py:13`）都会触发上述全部行为。

### 4.2 两套调度器配置矛盾，且存在重复任务

`app/scheduler.py:267-284` 把**两个不同任务**挂在**完全相同**的 Cron 上（JST 8/12/16/20 点）：

```python
scheduler.add_job(full_crawl,        trigger=CronTrigger(hour='8,12,16,20', ...))
scheduler.add_job(crawl_market_data, trigger=CronTrigger(hour='8,12,16,20', ...))
```

而 `full_crawl()` 的**最后一行**（`scheduler.py:260`）已经 `await crawl_market_data()`。结果是每次整点：抓到一半跑一次行情、结束后再跑一次行情 → 重复的外部请求与重复的 `advance()` 状态推进（虽然幂等，但会产生混乱日志与多余落库判断）。`docs/scheduler_guide.md:6-9` 又声称新闻是「每 3 小时 8 次」——与代码不符。

### 4.3 时区语义不一致

- `scheduler.py:31-33`：调度器用 `Asia/Tokyo`。
- `crud.py:31`：`datetime.now() - timedelta(hours=24)` 用**服务器本地时间（无时区）**。
- `bbcd.py`/`market_data.py:370`：行情用 `America/New_York`。
- 数据库 `publish_time` 是 naive `DATETIME`，BBC 的 `_parse_publish_time` 会把带时区的 RSS 时间 `dt.replace(tzinfo=None)` **直接丢弃时区**（`bbc.py:96`）。

三种时区观念混用且丢弃 tzinfo，导致「最近 24 小时」的边界在跨时区场景下不准确。

### 4.4 配置层系统性失效

实测审计结果（`app/config.py` 属性 × 全仓使用点）：

- **定义了但全仓 0 引用**：`DEBUG`、`HOST`、`PORT`、`FEISHU_WEBHOOK_URL`、`FEISHU_SECRET`、`FEISHU_KEYWORD`、`LOGS_DIR`。
- **`.env` 中配置但代码 0 引用**：`CRAWL_INTERVAL_HOURS`（文档说 6 小时，代码写死 4 次/天）、`IMAGES_DIR`、`FINNHUB_API_KEY`、`INDEX_FEISHU_WEBHOOK_URL`、`INDEX_KEYWORD`、`CLS_FEISHU_*`。
- **代码需要但 `.env.example` 未列**：`DFCF_FEISHU_KEYWORD/WEBHOOK_URL`、`KB_API_KEY`、`KB_MODEL_ID`、`KB_REGION`、`NYT_API_KEY`、`OPENROUTER_API_KEY`、`OPENROUTER_FEISHU_WEBHOOK_URL`、`OPENROUTER_KEYWORD`；`START_SCHEDULER`（`main.py:69` 的关键开关）在示例中完全没有。
- **`.env.example` 变量名写错**：`:36-40` 写的是 `KB_ACCOUNT_ID/KB_APIKEY/KB_SERVICE_RESOURCE_ID/KB_DOMAIN`，而代码读的是 `KB_API_KEY/KB_MODEL_ID/KB_REGION`。
- **`load_dotenv(override=True)`**（`config.py:5`）：环境变量会被 `.env` 覆盖，容器/CI 注入的配置失效。
- **无 Pydantic Settings 校验**：`PORT` 写错只会静默回落默认值；缺失必填项（如 `DEEPSEEK_API_KEY`）只在运行时表现为「分析器为 None」。
- **`RELOAD_INDEX_DATA=1` 当前处于开启状态** → `main.py:33-37` 每次启动都清空并重导 `index_history`（2967 行），与「仅在表为空时导入一次」的设计意图相反。

### 4.5 `crud.py` 的具体缺陷

| 位置 | 问题 |
|---|---|
| `crud.py:37,47` | `if include_keywords and include_keywords:` 冗余双重判断 |
| `crud.py:31` | 24 小时截断硬编码，`settings.NEWS_TIME_RANGE_HOURS` 未使用 |
| `crud.py:31,56` | `publish_time >= cutoff` + `order_by(publish_time)` **无索引**（`models.py:14` 未给 `publish_time` 建索引），全表扫描 + 排序 |
| `crud.py:42-43` | 对 `content`（TEXT 大字段）做 `contains` 全模糊匹配，无 FTS |
| `crud.py:86-89` | 「先查存在再插入」竞态：两个抓取任务（并发上限 `CONCURRENT_CRAWLERS=2`，`scheduler.py:37`）可能同时通过检查，最终靠 `url UNIQUE` 约束抛 `IntegrityError`，而该异常会一路冒泡到 `scheduler.py:107` 的兜底 `except`，导致**整源剩余新闻全部丢弃** |
| `crud.py:119` | `import logging` 写在函数体内（`:119-120`） |
| `crud.py:118-160` | 清洗逻辑与 3.3 的 SQL 错误叠加 |
| `crud.py:163-177` | `get_latest_market_prices` 对每个 symbol 单独发一条 SQL（N+1），可一次窗口函数查出 |
| `crud.py:190-197` | `get_market_peak` 按 `value DESC` 排序取首行——峰值语义与 `market_strategy_state` 的动态峰值**重复且可能不一致**，属死代码风险 |
| `crud.py:378-399` | `create_valuation_record` 每次插入都做全表 `DELETE` 裁剪到 100 条，`created_at` 无索引 |

### 4.6 错误处理与可观测性

- **HTTP 层永远返回 200**：`market.py:82-84`、`index_alarm.py:109-111`、`finance.py:96-98,137-139`、`valuation.py:105-107` 等大量 `except Exception: return {"status":"error", ...}`。前端与监控无法区分成功与失败，也无法走标准 5xx 告警。
- **异常信息直接回传前端**：`{"message": str(e)}` 会把内部路径、SQL、库名暴露给浏览器。
- **日志设施废弃**：`app/config.py:38` 定义 `LOGS_DIR` 并在 `:86` 创建 `logs/` 目录，但**全仓无任何 FileHandler**，目录为空；只有 `logging.basicConfig` 输出 stdout。README:72-73 承诺的 `logs/app.log` 不存在。
- **无请求日志/耗时/追踪 ID**，无 `/metrics`。
- `scheduler.py:194-219` 的 `_analyze_and_notify` 引用 `nonlocal total_analyzed`，而 `total_analyzed = 0` 在**函数定义之后**的 `:219` 才赋值；且 `scheduler.py:235` 在 `await` **之前**读取 `openrouter_analyzer.last_used_model`，导致首轮永远是空串（模型名永不展示）。

### 4.7 财务指标链路的正确性缺陷（实测复现）

用合成记录实际运行 `compute_indicators` 得到的可复现问题：

| 位置 | 问题 | 实测表现 |
|---|---|---|
| `finance_indicators.py:49` | `int(date_str[5:7])` 无校验 | 报告期写成 `2024-6-30`（未补零）直接抛 `ValueError`，整次查询 500 |
| `finance_indicators.py:225` | 环比基数直接取 `i-1`，**不校验是否紧邻上一季** | 仅有两期且相隔 6 个月（2023-12-31、2024-06-30）时，仍标为「环比增速 20.0」 |
| `finance_indicators.py:111-127` | 单季还原要求「同年 + 紧邻上一季」，否则整列 None | 同样两期数据下，营收/成本/净利/经营现金流**全部为 None**，而周转天数却有值（口径自相矛盾） |
| `finance.py:120-124` | 抓取时**先按 start/end 截断再 upsert 入库** | DB 里可能只剩年报期 → 上一条的 None 成为常态。应改为「入库全量、仅在展示层截断」 |
| `finance_indicators.py:171-176` | 平均余额不校验是否紧邻期 | 跨半年两期也算出「存货周转天数 44.2 天」 |
| `finance_indicators.py:53-55` | `_to_yi` 保留 1 位小数 | 小于 0.05 亿的值被抹成 `0.0`，与真实 0 无法区分 |
| `finance_indicators.py:241` | % 类指标实际算**百分点差**（`:82-86`），字段名仍为「同比增速」 | 毛利率 40.0→41.7 返回 1.7，但标签误导（前端 `finance.html:143` 也据此写"同比(百分点)"） |
| `finance_indicators.py:42-44` | `getattr(rec, attr, None)` 容错 | 模型列改名/缺失时**静默变 None**，不报错 |
| `getAkshare.py:52` vs `:60` | 前者只保留 df 中存在的列，后者硬索引全部 `FIELD_MAP.values()` | 任一报表缺列（如"交易性金融资产""利息支出"）→ 立即 KeyError，整次抓取失败（当前被 `finance.py:137-139` 兜成 HTTP 200 + `status:error`） |
| `getAkshare.py:14,49` | 无 try/except、无超时；akshare 内部 `requests.get` 亦无 timeout | 新浪挂起会**长期占死 FastAPI 线程池 worker** |
| `getAkshare.py:11-27` vs `crud.py:280-296` | 同一个 15 项字段映射**两份平行维护**（同序） | `crud.py:326-327` 只写 `FINANCIAL_FIELD_COLUMNS` 的键 → 只改一处会静默丢值 |
| `models.py:112` | `shareholders_equity` | akshare 全量写入、`finance_indicators` 读 14 列，此列入库后**无人使用**（死列） |
| `models.py:90` | 注释只写「金额单位：元」 | 未标注「利润表/现金流列是**累计值**、资产负债表是**时点值**」——而全部单季还原逻辑依赖这一区分 |

另外两页对同一数据的**量纲不一致**：`finance.html:102` 标「货币单位：亿元」（已 `_to_yi`），`crawl_control.html:374` 标「金额单位：元」（原始 records）。同一后端数据在两个页面显示相差 1e8 倍。

依赖成本：实测 `import akshare` 耗时 **6.37 秒**（`getAkshare.py:6` 顶层导入）。`finance.py:116` 已在端点内延迟导入，故首次 `/api/finance/fetch` 会额外阻塞 6 秒以上；而 `requirements.txt:21` 仍把 akshare 列为**强制依赖**（重依赖，非可选）。

### 4.8 前端重复与生命周期（摘要）

- **`escapeHtml` 定义了 4 份**（`main.js:29`、`index.html:111`、`news_detail.html:70`、`market.html:359`）；`crawl_control.html` / `finance.html` / `valuation.html` 靠"全局变量"隐式依赖 `main.js`，依赖关系不可见。
- **`formatTime` 3 份 + 1 份同名不同语义**（`valuation.html:178` 用字符串截取，其余用 `Date` 解析），全局函数互相覆盖。
- **`debounce` 2 份**、**`lazyInit` 2 份逐字相同**（`index_alarm.html:151` / `finance.html:57`）。
- **图表生命周期两套互斥实现**：`index_alarm.html` 只 `push` 到 `charts[]` 供 resize，**从不 dispose**；`finance.html` 有 `disposeCharts` 却每次查询重新 `lazyInit`，IntersectionObserver 只在自己触发后 disconnect（`:60-68`），快速连点存在 observer 泄漏与重复初始化竞态。
- **轮询永不停止**：`crawl_control.html:303-313` 固定 `setInterval(..., 500)`（2 req/s），`isCrawling` 只在 `updateStatus`（`:269`）成功时更新，而 `loadStatus` 吞掉异常（`:236-238`）→ 一旦 `/api/crawl/status` 持续失败，定时器将永久运行；无 `pagehide` 清理。
- **`<style>` 出现在 `{% block scripts %}` 内**（`market.html:389`、`crawl_control.html:72`、`finance.html:229`、`index_alarm.html:275`、`valuation.html:218`），实际渲染在 `<body>` 末尾 → 无效位置、FOUC。
- **CSS 类名互相覆盖**：`.loading/.error-state` 在 `style.css:591`、`crawl_control.html:214`、`finance.html:270` 三处定义且错误色不同（`#721c24` vs `#f14c4c`）；`.btn-*` 在 `style.css:301-332`（渐变）与 `valuation.html:261-267`（纯色）冲突；`style.css` 无任何 CSS 变量。
- **首页新闻源列表硬编码在前端**（`index.html:11-17`：财联社/新华网/东方财富/36氪/巨潮），与后端真实源（东方财富/NYT/BBC）完全不符 → 筛选器选了也永远查不到数据。
- **资源策略不统一**：echarts 本地 vendored，Chart.js 走公网 CDN 且无 `integrity`（`market.html:40`），离线/内网环境直接失效。
- **`login.html` 不继承 `base.html`**（自带 102 行内联 CSS、不加载 `main.js`），是 8 个页面里唯一的例外。
- **三处已鉴权 GET 走裸 `fetch`**：`index.html:44`（401 被 `data.news||[]` 吞掉 → 静默显示"暂无新闻数据"）、`news_detail.html:21`（401 被误报为"新闻不存在"）、`crawl_control.html:232`（仅 console.error）。`main.js:48-55` 的 `fetchWithAuth` 是唯一 401 出口，但这三处绕过了它。`/api/check-auth` 已实现却从未被调用。

### 4.9 依赖与工程质量

`requirements.txt` 用 `>=` 松约束（无 lock 文件，构建不可复现），且**缺失实际被 import 的包**：

| 缺失包 | 使用位置 |
|---|---|
| `feedparser` | `app/crawlers/bbc.py:3` —— 未声明，且出现在 `except Exception` 之外，缺失会直接 ImportError |
| `pandas` | `getAkshare.py` |
| `hypercorn` | `start_with_hypercorn.py:8`（该脚本必挂） |
| `pytest` / `pytest-asyncio` | 无测试依赖，却有两个 `test_*.py` |

其他：无 `pyproject.toml`、无 ruff/black/mypy、无 CI、无 `Dockerfile`；`index/chart_2015.html`（355KB）是把 CSV 渲染成图表后的**生成物**却被提交进仓库。

**文档脱节清单**（`README.md` 与 `需求文档.md` 描述的是另一个项目，`.trae/specs/` 下 3 份规格笔记同样与现状不符）：

| 文档声称存在 | 实际情况 |
|---|---|
| 数据源：财联社、新华网、东方财富、36氪、巨潮资讯、Finnhub | 实现的是东方财富、NYT、BBC + Nasdaq/CBOE/Treasury；财联社/新华网/36氪/巨潮/Finnhub **完全不存在**（`FINNHUB_API_KEY`、`CLS_FEISHU_*` 仍在 `.env` 中空置） |
| `app/api/filter.py`、`app/api/logs.py` | 不存在（筛选规则与抓取日志功能已分别被移除） |
| `app/crawlers/{cls,xinhua,eastmoney,kr36,cninfo}.py` | 不存在（只有 `eastmoney_depth.py`） |
| `app/utils/{filters,image_downloader}.py`、`static/images/` | 不存在（图片改存 `data/images/`，且无下载器） |
| 表 `filter_rules`、`crawl_logs`、`index_highs` | 表还在磁盘上但**代码已无模型**（孤儿表） |
| `/api/push/feishu`、`/api/ai/score`、`/api/ai/investment` | 实际为 `/api/feishu/push`；两个 AI 接口从未实现 |
| 「每 6 小时抓取」 | 代码是每天 4 次（JST 8/12/16/20），`CRAWL_INTERVAL_HOURS` 未被引用 |
| 「图片自动下载并保存到本地」 | 无任何图片下载代码；`NewsItem.image_url` 恒为 `None`（`scheduler.py:67` 写死 `image_path=None`） |
| `logs/app.log` | 无 FileHandler，目录为空 |
| 「定时抓取 + 手动抓取」 | 成立，但 `START_SCHEDULER` 默认为 `False`（`config.py:77`），Web 进程需配合 `run_scheduler.py` 才有定时任务 |

---

## 五、根目录 46 个脚本的清理清单

| 分类 | 文件 | 处置建议 |
|---|---|---|
| **正式入口** | `run_scheduler.py`（唯一且正确：有 `__main__` guard、有优雅关闭尝试） | 保留 |
| | `getAkshare.py` —— **活代码**，被 `app/api/finance.py:116` 直接 `from getAkshare import ...` | 迁入 `app/utils/akshare_reports.py` 并改导入 |
| **重复启动器（11 个）** | `run_server.py`、`launch.py`、`final_start.py`、`start_app.py`、`direct_start.py`、`subprocess_start.py`、`start_with_hypercorn.py`、`minimal_test.py`、`ultra_simple.py` | 删除。统一为 `uvicorn app.main:app --host $HOST --port $PORT`；端口 8000 在 11 个脚本里各写一遍却都忽略 `.env` 的 `HOST/PORT` |
| **重复业务逻辑** | `run_crawl.py`、`run_crawl_new.py`（互为副本，且**模块级** `asyncio.run(full_crawl())`，被 import 即触发全量抓取）、`sqlite_query.py`、`init_db.py`（建表已被 `app/main.py:28-29` 覆盖） | 删除，改用 `python -m app.scheduler` / CLI 子命令 |
| **一次性工具** | `get_x_id.py`、`openrouter_free_models.py`、`run_deepseek.py`、`run_doubao.py`、`run_xtwee.py` | 收拢到 `tools/`。其中 `run_doubao.py:40-43`、`run_deepseek.py:48-51` 会**把 .env 中含 API Key 的整行 print 到 stdout**（`print(f"     L{i}: {repr(line.rstrip())}")`），必须删除该逻辑；`run_doubao.py:85-139` 内联复制了 `app/utils/doubao_analyzer.py` 的完整 prompt；`run_xtwee.py:40-82` 重实现 `app/crawlers/x_twitter.py:95` 的抓取+去重+推送 |
| **调试残留** | `em_test.log`（指向已不存在的 `test_eastmoney.py` 的超时堆栈） | 删除 |
| **伪测试** | `test_market_strategy_fixed.py`、`test_vix_level.py`（自执行 assert，非 pytest，`sys.path.insert`） | 改写为 `tests/` 下的 pytest 用例 |

另注：`.gitignore:61-75` 把 `test_*.py`、`start_*.py`、`run_crawl*.py` 等 13 类文件**预先忽略**——这是"用 gitignore 掩盖垃圾文件"的反模式，会连带阻止未来真正的 `tests/` 入库，应删除该段规则。

Shell 脚本问题：`start.sh` **全文 CRLF**（实测 153 行全部 CRLF）→ Linux 上 `#!/bin/bash` 会 `bad interpreter: ^M`；`start.sh:51,55` 与 `run.sh:28,33` 使用 `venv/bin/activate`，而仓库里只有 `.venv/`（会静默新建第二套环境并重装依赖）；`start.sh:131` 用 Ubuntu 专有的 `ss -ltnp`；`start.ps1:7` 无 `Set-Location $PSScriptRoot`，依赖调用者 cwd。全部 11 处端口硬编码且忽略 `.env`。

---

## 六、git 与仓库卫生

- **分支分叉**：本地 `master` 领先 `origin/main` **293 个提交**（`git rev-list --left-right --count master...origin/main` → `293  1`），而 `origin/HEAD → origin/main`。上游配置为 `origin/master`，远程同时存在 `main` 与 `master` 两条线 → 极易误推/误合并。
- **`backup/` 反向忽略规则失效**：`.gitignore:36-37` 忽略 `*.sqlite3`，`:40-42` 又写 `!backup/` + `!backup/*.sqlite3` 试图放行。实测 `git check-ignore -v backup/db_20260916_161535.sqlite3` 命中 `:42`，即**确实被放行** → 一个 548KB 的**完整生产数据库**（含 2967 行指数数据）会随 `git add -A` 进入公共仓库。`app/utils/local_backup.py:4-5` 的注释"备份文件纳入 git 管理"证实这是有意为之，但方向错误。
- **备份无价值**：`backup/db_20260916_161535.sqlite3` 与当前 `data/db.sqlite3` 逐表行数完全一致（news 0 / market_prices 11 / index_history 2967）——备份时库已 3 天未变。备份应存到仓库外（或 Git LFS / 对象存储），并加密。
- **未覆盖的忽略项**：`data/market_strategy.json`（含策略业务配置）、`.trae/`、`.workbuddy/`、`docs/` 未被 `.gitignore` 覆盖。
- **死文件**：`data/event_drive.db`、`data/market.db` 均为 **0 字节**（残留）；`data/db.sqlite3` 是唯一活跃库（`settings.DATABASE_URL` 实测 = `sqlite:///./data/db.sqlite3`）。

---

## 七、改进路线图

> 按「风险 × 紧迫度」排序。每项均只给方案，不含代码改动。

### P0 — 立即（建议 1-2 天内完成，可独立上线）

0. **密钥轮换与历史清理**：轮换 `.env` 中全部真实密钥（OpenRouter、DeepSeek、火山方舟 KB、NYT、Finnhub、X 四件套）与 **10 个飞书 webhook token**；清理 `.env.example:70` 中残留的 webhook；用 `git filter-repo --path .env --invert-paths` 重写历史后强推，并通知所有协作者重新克隆（重写历史前先确认无人基于旧历史工作）。
1. **补鉴权**：给 `POST /api/crawl/trigger`（`crawl.py:34`）与 `POST /api/logout`（`login.py:61`）加 `Depends(require_auth)`；建议进一步给**所有**写接口统一挂一个 router 级依赖，避免下次再漏。
2. **修 `cleanup_sparse_market_dates`**：把 `func.count(func.nullif(value, None))` 改为 `func.count(models.MarketPrice.value)`；同时把该接口改为「先返回将要删除的条数供前端确认」的两阶段调用，并加软删除/备份兜底。**在此之前请勿点击页面的「清洗数据」按钮。**
3. **修 XSS**：`crawl_control.html:262` 改为 `textContent` 或 `escapeHtml`；补 `index.html:65`、`news_detail.html:31/43/51`、`market.html:83/87/89/92/119-121`、`finance.html:172`；新增属性专用转义（显式替换 `&<>"'`）用于 `value=` / `alt=` 位置；`main.py:93` 加 `autoescape=True`。
4. **修分页**：`/api/news` 改为返回 `{items, total, skip, limit}`（保持旧字段兼容一个版本），`crud.get_news_list` 增加配套 `count_news` 查询。
5. **登录加固**：`SECRET_KEY` 改从 `.env` 读取并校验非默认值；`set_cookie` 加 `secure=True`；token 内加入 `iat`/`exp` 并在校验时验证；把所有写接口纳入 CSRF 双提交 token（`fetchWithAuth` 统一注入 `X-CSRF-Token`）；长期方案是改用 Starlette `SessionMiddleware` + `passlib`/`argon2`，用户表入 DB。

### P1 — 近期（1-2 周）

6. **统一时间解析，再让 `BaseCrawler` 生效（顺序不可颠倒）**：先把 `nytimes.py:102/140/343/381` 四份重复的解析函数收敛为一个「返回 naive-UTC」的实现（删掉 `:147-158` 的 aware 分支），使 `base.py:47-48` 的比较可安全调用；然后删除 4 个子类的 `crawl()` 覆盖，改为在子类只实现 `fetch_news_list` / `fetch_news_detail` / `parse_news_item` 三个 abstract 方法，把「条数上限」与「是否启用时间窗」作为基类可选参数（`max_items` / `use_time_filter`）显式声明，让 NYT «12 条» 这类差异不再硬编码；统一改 `asyncio.get_running_loop()`。同时删除 `NYTCrawler`（无实例化点的死代码）、给 wire+topstories 合并加 url 去重、给两次 NYT API 调用之间加延时。
7. **让异常可见**：`self.error_message` 有 7 处赋值却全仓 0 处读取——要么在 `crawl_single_source` 失败分支中记录它，要么直接改用 `logger`；`nytimes.py` 全文无 `logging` 导入，必须补上，否则 NYT_API_KEY 缺失这类配置错误永远不可见。
8. **修财务指标口径**（详见 §4.7）：`_quarter_no` 加正则校验；环比与平均余额都要求「紧邻上一季」否则置 None 并标注；`finance.py:120-124` 改为入库全量、仅展示层截断；启用 `INDICATOR_DEFS` 里已存在但未使用的口径元数据（`_kind`）做断言；`getAkshare.py:52` 与 `:60` 使用同一列集合，缺列抛明确异常并补超时；合并 `getAkshare.py:11-27` 与 `crud.py:280-296` 的重复字段映射为单一模块；统一 `finance.html` 与 `crawl_control.html` 的量纲。
9. **修新闻表结构**：`News` 增加 `news_type` 列 + 一次显式迁移（并清理 `crawl_logs`/`filter_rules`/`index_highs` 三张孤儿表）；给 `publish_time` 与 `(source, publish_time)` 加索引；把 `is_news_exists` + `create_news` 换成「先尝试插入、捕获 `IntegrityError` 后跳过」，消除并发竞态导致的整源丢弃。
10. **引入 Alembic** 替代 `ensure_schema_compatibility` 的 4 处手写 DDL；把导入期副作用（建表、CSV 导入、notifier 初始化）全部移入 `lifespan`。
11. **配置层重做**：改用 `pydantic-settings` 的 `BaseSettings`（带类型、默认值、必填校验，`.env` 与真实环境变量分层，去掉 `override=True`）；建立**单一配置来源**，用一个脚本对比 `.env.example` ↔ `Settings` ↔ 代码引用，消除当前 10+ 处漂移；删除 7 个 0 引用配置项或补上使用点；`RELOAD_INDEX_DATA` 改回 0 并纳入一次性 CLI 命令 `python -m app.cli reload-index`。
12. **统一错误语义**：所有 API 改为抛 `HTTPException`（或注册全局 `exception_handler`），返回标准 `{error: {code, message}}` 且**不回传 `str(e)`**；`MarketPrice` schema 修正继承 `MarketPriceBase`；`schemas.py` 与 `models.py` 之间加一个断言式一致性测试（用 `__table__.columns` 对比 schema 字段）。
13. **调度器整理**：删掉与 `full_crawl` 末行重复的 `market_crawl_job_daily_4_times`；Cron 表达式与 `docs/scheduler_guide.md` 同步；时区统一——DB 存 UTC、展示层按 `Asia/Tokyo`（或国内场景 `Asia/Shanghai`）转换，`publish_time` 保留 tzinfo；修 `_analyze_and_notify` 的 `nonlocal` 顺序与 `last_used_model` 的读取时机。
14. **异步与资源生命周期**：
    - `market_data.py:33` 的模块级 `httpx.AsyncClient` 移入 lifespan 并在关闭时 `aclose()`；给所有请求加 `httpx.Timeout(connect=5, read=20)`。
    - 给 3 个分析器加 `aclose()`，`init_*_analyzer` 移出 `full_crawl()` 改为生命周期单例（当前**每轮抓取都新建连接池并丢弃旧的**）；OpenRouter 显式 `AsyncOpenAI(timeout=60, max_retries=1)`（当前用 SDK 默认 ≈600s 读超时）。
    - `feishu_notifier`：消除"直发/入队"双路径，统一入队 + `Queue(maxsize=N)` + 满时计数丢弃；冷却时间改为 `dict[channel, last_ts]` 消除竞态窗口（当前在 `finally` 释放锁后才 `await post`，限流实际失效）；关闭预算按队列长度计算（当前固定 5s 会静默丢弃剩余消息）；接通从未启用的 `secret` 签名参数（`init_all_notifiers` 对 7 个通道一律传 `secret=""`）。
    - `EastmoneyDepthCrawler` 每个周期启动一个 Chromium（`asyncio.to_thread(sync_playwright)`），与 `CONCURRENT_CRAWLERS=2` 叠加有资源风险——改为共享一个 async Playwright 实例，或直接改用其 JSON 接口（httpx）。
    - 删除根脚本里把 `.env` 整行（含 API Key）`print` 到 stdout 的调试代码（`run_doubao.py:40-43`、`run_deepseek.py:48-51`）。
15. **前端结构收敛**（详见 §4.8，分步做，无需构建工具）：抽 `static/js/modules/{api,dom,format,ui,charts}.js` 用 `type="module"`；消除 4 份 `escapeHtml` / 3 份 `formatTime` / 2 份 `debounce` / 2 份 `lazyInit`；页面 `<style>` 全迁 `style.css` 并引入 `:root` 变量；`base.html` 增加 `{% block head %}`/`{% block styles %}`；把 3 处裸 `fetch` 换成 `fetchWithAuth`；修 `crawl_control.html` 的轮询（指数退避 + 连续失败即停 + `pagehide` 清理）与 `index_alarm.html` 的 dispose 缺失；`login.html` 改为继承精简 base。
16. **可观测性**：接 `logging.handlers.RotatingFileHandler` 写入 `logs/app.log`（兑现 `config.py:38` 与 README 的承诺）、`logging.dictConfig` 统一格式；请求日志中间件（方法/路径/耗时/状态）；APScheduler 任务失败钩子（`EVENT_JOB_ERROR`）推到飞书。
17. **清理与整合**：按 §5 表删除 20 个文件、迁移 1 个、收拢 5 个到 `tools/`；修 `start.sh` 的 CRLF 与 `venv` 路径、`start.ps1` 加 `Set-Location $PSScriptRoot`、端口统一读 `.env`；改 `.gitignore`（删预忽略段、把 `backup/*.sqlite3` 恢复为忽略、补 `data/market_strategy.json`）。
18. **仓库治理**：决定 `main` / `master` 唯一主线并删除另一条（当前分叉 293 提交）；`.env.example` 补全所有变量并修正 KB_* 命名；把 `backup/` 移出仓库；`index/chart_2015.html` 移出仓库或改为运行时生成。

### P2 — 中期（1 个月+）

19. **测试体系**：新增 `pyproject.toml` + `tests/`（pytest + pytest-asyncio + httpx `ASGITransport` + 内存 SQLite fixture）；优先覆盖 5 个高价值纯函数——`market_strategy.compute_drawdown_events` / `compute_level`（已有 2 个朴素测试可迁移）、`finance_indicators.compute_indicators`（纯函数，本次评审用合成记录跑出的用例可直接转成 fixture：Q1 直取 / Q2-Q4 相减 / 跨年 None / 缺季 None / 未补零日期 / base=0 与负基数 / 分母为 0 / 切片对齐）、`valuation._compute_valuation`、`crud.cleanup_sparse_market_dates`（§3.3 的 bug 正是缺这个测试造成的）。加 `requirements-dev.txt` 与 CI（ruff + pytest）。
20. **依赖治理**：补齐 `feedparser`/`pandas`；把 akshare（导入耗时 6.37s 的重依赖）降为可选并隔离到 `app/services/financial_source.py`（定义 `FinancialSource` 协议 + fake/CSV 实现，依赖注入，未安装时返回明确提示，保留根级 `getAkshare.py` 作薄适配器）；锁定版本（`uv lock` / `pip-compile`）；拆 `requirements.txt`（web / crawler / llm / akshare 四组）。
21. **`crud.py` 拆分**：420 行的单文件按域拆为 `repositories/{news,market,index,finance,valuation}.py`；`get_latest_market_prices` 改单条窗口函数 SQL；新闻检索加 SQLite FTS5 全文索引替代 `content LIKE %kw%`；`get_market_peak`（`crud.py:190-197`，与 `market_strategy_state` 的动态峰值语义重复）明确废弃或复用。
22. **数据层演进**：评估 SQLite → PostgreSQL（当前 WAL + `busy_timeout=5000` 已接近 SQLite 并发上限，且调度器与 Web 是**两个进程**同时写同一文件）；至少要把备份改为「每日定时 + 保留 N 份 + 存仓库外 + 校验」。
23. **产品层补全**：让前端新闻源列表由后端提供（`/api/meta` 或在页面用 `|tojson` 注入），消除 §4.8 的硬编码不一致；`news_type` 落地后恢复「NYT 最新资讯 / 精选」分类展示；补齐 README/需求文档（当前描述的 5 个数据源、`filter_rules`/`crawl_logs` 表、`/api/push/feishu`、`/api/ai/score`、`/api/ai/investment`、`app/api/filter.py`、`app/crawlers/cls.py` 等**全部不存在**）。

---

## 八、明确不建议做的事

- **不要改成 SPA**。8 个页面、约 1,500 行 JS，Jinja + 每页 `fetch` 的模型够用；重写收益低、回归风险高。若要提升首屏，用 `|tojson` 把列表**服务端首屏渲染**即可，同时天然解决「401 时白屏或显示'暂无数据'」。
- **不要引入 Celery/Redis 替换 APScheduler**。单机、4 次/天的负载用不上；真正需要的是 §P1-11/12 的重复任务、时区、生命周期修复。
- **不要在修好 §3.3（清洗接口）之前动 `market_prices` 的历史数据**。
- **不要为了"统一"而删掉 `market_strategy.py`**。峰值回撤状态机（`advance` 持久化峰值 + `compute_drawdown_events` 回扫历史）设计清晰、注释完整、是仓库里质量最高的模块，应作为其他模块（尤其 `crud.py`）的注释与拆分参照。

---

## 九、附：本次分析中已实测验证的关键事实

| 结论 | 验证方式 |
|---|---|
| `news` 表 0 行、`market_prices` 11 行、`index_history` 2967 行 | 直接查 `data/db.sqlite3` |
| `news` 表无 `news_type` 列；库中存在 `crawl_logs`/`filter_rules`/`index_highs` 孤儿表 | `PRAGMA table_info` + `sqlite_master` |
| `COUNT(NULLIF(value, NULL))` 编译为 `count(nullif(market_prices.value, NULL))` | SQLAlchemy sqlite dialect 编译输出 |
| `schemas.MarketPrice` 字段仅 `id/created_at/updated_at` | 导入后读 `model_fields` |
| `.env` 曾 10 次进入版本控制且可达 `origin/master` | `git log --all -- .env`、`git branch -r --contains` |
| `master` 领先 `origin/main` 293 个提交 | `git rev-list --left-right --count` |
| `backup/*.sqlite3` 实际未被忽略 | `git check-ignore -v` 命中 `.gitignore:42` |
| 备份库与活跃库逐表行数完全一致 | 分别查询两库计数 |
| 4 个爬虫全部覆盖 `crawl()`，`NEWS_PER_SOURCE`/时间窗失效 | grep + 逐文件核对 |
| `DEBUG/HOST/PORT/FEISHU_*/LOGS_DIR` 全仓 0 引用；10 个 `.env` 变量代码未用 | 全仓 AST 级正则审计 |
| `start.sh` 全文 CRLF；`venv/` 路径不存在 | 字节级换行统计 + 目录枚举 |
| 应用可正常 import，共 22 个 API 路由 + 10 个页面路由 | 导入 `app.main:app` 后枚举 `app.routes` |
| 东方财富抓取真实超时失败 | `em_test.log` 中的 Playwright `TimeoutError` 堆栈 |
| NYT 的 `fetch_news_detail`、`_parse_publish_time`、`NYTCrawler` 均无调用者；`self.error_message` 有 7 处赋值、0 处读取 | 全仓引用检索 |
| `compute_indicators` 的季度还原/环比/周转缺陷 | 用合成记录实际运行该函数复现（无文件改动） |
| `import akshare` 耗时 6.37 秒 | 计时导入 |

## 十、附：本次分析未覆盖的部分

- `static/js/echarts.min.js`（1MB vendored 库）仅确认引用位置，未审阅其内容。
- `.trae/specs/` 下的 3 份 AI 规格笔记（`news-crawler-app` / `x-twitter-crawler` / `feishu-interactive-bot`）与 `.workbuddy/` 未逐字审阅；抽查发现其描述的模块划分与现状不符，已在 §4.9 一并归入「文档脱节」。
- `data/images/` 下 2 张已入库图片未校验内容与引用关系。
- 上一节所有结论均为**静态分析 + 针对性实测**，未做端到端运行验证（未启动服务、未触发真实抓取，以避免产生外部副作用与写入生产库）。

