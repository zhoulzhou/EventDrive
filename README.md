# EventDrive - 新闻抓取应用

一个基于Python的新闻抓取应用，支持从5大权威网站自动抓取新闻，通过Web界面查看和管理。

## 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| 后端框架 | FastAPI | 现代高性能，异步友好 |
| 数据库 | SQLite | 轻量，无需额外安装 |
| 模板引擎 | Jinja2 | FastAPI自带 |
| 定时任务 | APScheduler | Python定时任务库 |
| 反爬虫方案 | Playwright | 轻量自动化浏览器 + 请求头伪装 + 延时 |
| 前端 | HTML + CSS + JavaScript | 配合Jinja2模板 |

## 数据来源

1. 财联社
2. 新华网
3. 东方财富
4. 36氪
5. 巨潮资讯（A股公告）

## 项目结构

```
EventDrive/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI入口
│   ├── config.py               # 配置文件
│   ├── database.py             # 数据库连接
│   ├── models.py               # 数据模型
│   ├── schemas.py              # Pydantic模式
│   ├── crud.py                 # 数据库操作
│   ├── api/
│   │   ├── __init__.py
│   │   ├── news.py             # 新闻相关API
│   │   ├── crawl.py            # 抓取相关API
│   │   ├── filter.py           # 筛选规则API
│   │   └── logs.py             # 日志相关API
│   ├── crawlers/
│   │   ├── __init__.py
│   │   ├── base.py             # 爬虫基类
│   │   ├── cls.py              # 财联社爬虫
│   │   ├── xinhua.py           # 新华网爬虫
│   │   ├── eastmoney.py        # 东方财富爬虫
│   │   ├── kr36.py             # 36氪爬虫
│   │   └── cninfo.py           # 巨潮资讯爬虫
│   ├── scheduler.py            # 定时任务
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── anti_crawl.py       # 反爬虫工具
│   │   ├── image_downloader.py # 图片下载器
│   │   └── filters.py          # 筛选工具
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── news_detail.html
│       ├── crawl_control.html
│       ├── filter_rules.html
│       └── crawl_logs.html
├── static/
│   ├── css/
│   ├── js/
│   └── images/
├── data/
│   ├── db.sqlite3              # SQLite数据库
│   └── images/                  # 下载的图片
├── logs/
│   └── app.log
├── requirements.txt
├── .env.example
└── README.md
```

## 安装说明

### Ubuntu 22.04 一键启动

```bash
# 克隆项目
git clone <repository-url>
cd EventDrive

# 运行启动脚本（自动创建虚拟环境、安装依赖、启动服务器）
chmod +x run.sh
./run.sh
```

### 手动安装（Ubuntu/Windows）

#### 1. 克隆项目

```bash
cd EventDrive
```

#### 2. 创建虚拟环境（推荐）

**Ubuntu:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows PowerShell:**
```bash
python -m venv venv
.\venv\Scripts\activate
```

#### 3. 安装依赖

```bash
pip install -r requirements.txt
```

#### 4. 安装Playwright浏览器（如需要）

```bash
playwright install
```

#### 5. 配置环境变量

**Ubuntu:**
```bash
cp .env.example .env
```

**Windows PowerShell:**
```powershell
Copy-Item .env.example .env
```

根据需要修改`.env`中的配置。

#### 6. 初始化数据库

```bash
python init_db.py
```

这将创建数据库表和默认筛选规则。

## 运行应用

### Ubuntu（推荐）

使用启动脚本：
```bash
./run.sh
```

### 开发模式

**Ubuntu:**
```bash
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Windows:**
```bash
.\venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 生产模式

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

应用启动后，访问 http://localhost:8000 查看Web界面。

## 功能特性

- **自动抓取**：每6小时自动从5大网站抓取新闻
- **手动抓取**：支持通过Web界面手动触发抓取
- **新闻管理**：查看、搜索和筛选新闻
- **筛选规则**：自定义包含和排除关键词筛选新闻
- **图片下载**：自动下载并保存新闻图片
- **抓取日志**：完整记录抓取历史
- **预留接口**：飞书推送、AI打分、投资决策建议

## Web界面

1. **首页/新闻列表页**：分页显示新闻列表
2. **新闻详情页**：展示完整新闻内容
3. **抓取控制页**：手动触发抓取，查看抓取状态
4. **筛选规则管理页**：配置包含/排除关键词
5. **抓取日志页**：查看抓取历史记录

## 后续扩展规划

### 第一阶段（已确认）
- 基础抓取功能
- Web界面
- 筛选功能

### 第二阶段（预留）
- 飞书推送
- AI新闻打分
- AI投资决策建议

### 第三阶段（可选）
- 更多数据源
- 新闻分类
- 用户系统
- 数据统计与可视化

## 许可证

MIT License
