# 新闻抓取应用 - The Implementation Plan (Decomposed and Prioritized Task List)

## [ ] Task 1: 项目初始化和目录结构搭建
- **Priority**: P0
- **Depends On**: None
- **Description**: 
  - 创建项目目录结构
  - 生成requirements.txt文件
  - 创建配置文件和环境变量示例
  - 创建README.md文档
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `programmatic` TR-1.1: 目录结构符合需求文档中的规范
  - `programmatic` TR-1.2: requirements.txt包含所有必要依赖
  - `human-judgement` TR-1.3: README.md包含项目说明和启动指南
- **Notes**: 依赖包括fastapi, uvicorn, sqlalchemy, apscheduler, playwright, python-multipart, jinja2等

## [ ] Task 2: 数据库模型和连接层实现
- **Priority**: P0
- **Depends On**: Task 1
- **Description**: 
  - 实现SQLAlchemy数据库连接配置
  - 定义News、FilterRule、CrawlLog三个数据模型
  - 实现数据库表创建脚本
- **Acceptance Criteria Addressed**: AC-2
- **Test Requirements**:
  - `programmatic` TR-2.1: 成功创建三张数据库表
  - `programmatic` TR-2.2: 模型字段与需求文档一致
  - `programmatic` TR-2.3: url字段有唯一约束用于去重

## [ ] Task 3: CRUD操作层实现
- **Priority**: P0
- **Depends On**: Task 2
- **Description**: 
  - 实现新闻的增删改查操作
  - 实现筛选规则的增删改查操作
  - 实现抓取日志的增删改查操作
  - 实现新闻去重检查功能
- **Acceptance Criteria Addressed**: AC-2
- **Test Requirements**:
  - `programmatic` TR-3.1: 能成功保存新闻数据
  - `programmatic` TR-3.2: 重复url的新闻不会被保存
  - `programmatic` TR-3.3: 能查询筛选规则
  - `programmatic` TR-3.4: 能保存和查询抓取日志

## [ ] Task 4: 爬虫基类和反爬虫工具实现
- **Priority**: P0
- **Depends On**: Task 3
- **Description**: 
  - 实现反爬虫工具类（请求头伪装、随机延时）
  - 实现Playwright浏览器自动化封装
  - 实现爬虫基类，包含通用抓取逻辑
  - 实现图片下载器工具
- **Acceptance Criteria Addressed**: AC-3
- **Test Requirements**:
  - `programmatic` TR-4.1: 请求头包含伪装的User-Agent等信息
  - `programmatic` TR-4.2: 请求之间有2-5秒随机延时
  - `programmatic` TR-4.3: 能成功下载并保存图片到本地

## [ ] Task 5: 财联社爬虫实现
- **Priority**: P0
- **Depends On**: Task 4
- **Description**: 
  - 实现财联社网站爬虫
  - 解析新闻标题、正文、发布时间、作者、摘要、图片等字段
  - 过滤最近24小时内的新闻
  - 限制每个网站最多10条
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `programmatic` TR-5.1: 能成功抓取财联社新闻
  - `programmatic` TR-5.2: 抓取的新闻字段完整
  - `programmatic` TR-5.3: 只抓取最近24小时内的新闻
  - `programmatic` TR-5.4: 最多抓取10条新闻

## [ ] Task 6: 新华网爬虫实现
- **Priority**: P0
- **Depends On**: Task 5
- **Description**: 
  - 实现新华网网站爬虫
  - 解析新闻标题、正文、发布时间、作者、摘要、图片等字段
  - 过滤最近24小时内的新闻
  - 限制每个网站最多10条
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `programmatic` TR-6.1: 能成功抓取新华网新闻
  - `programmatic` TR-6.2: 抓取的新闻字段完整
  - `programmatic` TR-6.3: 只抓取最近24小时内的新闻
  - `programmatic` TR-6.4: 最多抓取10条新闻

## [ ] Task 7: 东方财富爬虫实现
- **Priority**: P0
- **Depends On**: Task 6
- **Description**: 
  - 实现东方财富网站爬虫
  - 解析新闻标题、正文、发布时间、作者、摘要、图片等字段
  - 过滤最近24小时内的新闻
  - 限制每个网站最多10条
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `programmatic` TR-7.1: 能成功抓取东方财富新闻
  - `programmatic` TR-7.2: 抓取的新闻字段完整
  - `programmatic` TR-7.3: 只抓取最近24小时内的新闻
  - `programmatic` TR-7.4: 最多抓取10条新闻

## [ ] Task 8: 36氪爬虫实现
- **Priority**: P0
- **Depends On**: Task 7
- **Description**: 
  - 实现36氪网站爬虫
  - 解析新闻标题、正文、发布时间、作者、摘要、图片等字段
  - 过滤最近24小时内的新闻
  - 限制每个网站最多10条
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `programmatic` TR-8.1: 能成功抓取36氪新闻
  - `programmatic` TR-8.2: 抓取的新闻字段完整
  - `programmatic` TR-8.3: 只抓取最近24小时内的新闻
  - `programmatic` TR-8.4: 最多抓取10条新闻

## [ ] Task 9: 巨潮资讯爬虫实现
- **Priority**: P0
- **Depends On**: Task 8
- **Description**: 
  - 实现巨潮资讯网站爬虫（A股公告）
  - 解析公告标题、正文、发布时间、作者、摘要、图片等字段
  - 过滤最近24小时内的公告
  - 限制每个网站最多10条
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `programmatic` TR-9.1: 能成功抓取巨潮资讯公告
  - `programmatic` TR-9.2: 抓取的公告字段完整
  - `programmatic` TR-9.3: 只抓取最近24小时内的公告
  - `programmatic` TR-9.4: 最多抓取10条公告

## [ ] Task 10: 定时任务实现
- **Priority**: P0
- **Depends On**: Task 9
- **Description**: 
  - 集成APScheduler
  - 配置每6小时触发一次全量抓取任务
  - 实现任务状态跟踪
  - 在应用启动时自动启动调度器
- **Acceptance Criteria Addressed**: AC-5
- **Test Requirements**:
  - `programmatic` TR-10.1: 定时任务能正确注册
  - `programmatic` TR-10.2: 应用启动时调度器自动启动
  - `human-judgement` TR-10.3: 能观察到定时任务按预期执行

## [ ] Task 11: Pydantic Schemas定义
- **Priority**: P0
- **Depends On**: Task 2
- **Description**: 
  - 定义新闻相关的请求和响应Schema
  - 定义筛选规则相关的Schema
  - 定义抓取日志相关的Schema
  - 定义预留接口的Schema
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `programmatic` TR-11.1: 所有Schema定义完整
  - `programmatic` TR-11.2: Schema类型约束正确

## [ ] Task 12: FastAPI API端点实现（新闻相关）
- **Priority**: P0
- **Depends On**: Task 11, Task 3
- **Description**: 
  - 实现新闻列表API（分页、筛选）
  - 实现新闻详情API
  - 实现新闻筛选工具函数
- **Acceptance Criteria Addressed**: AC-6, AC-8
- **Test Requirements**:
  - `programmatic` TR-12.1: GET /api/news返回新闻列表（分页）
  - `programmatic` TR-12.2: GET /api/news/{id}返回新闻详情
  - `programmatic` TR-12.3: 筛选功能正确工作（包含/排除关键词）

## [ ] Task 13: FastAPI API端点实现（抓取和控制相关）
- **Priority**: P0
- **Depends On**: Task 12, Task 9
- **Description**: 
  - 实现手动触发抓取API
  - 实现抓取状态查询API
  - 实现筛选规则管理API（获取、更新）
  - 实现抓取日志查询API
  - 实现预留接口（飞书推送、AI打分、投资决策）
- **Acceptance Criteria Addressed**: AC-6, AC-9
- **Test Requirements**:
  - `programmatic` TR-13.1: POST /api/crawl/trigger能触发抓取
  - `programmatic` TR-13.2: GET /api/crawl/status返回抓取状态
  - `programmatic` TR-13.3: 筛选规则API能正常工作
  - `programmatic` TR-13.4: 抓取日志API能正常工作
  - `programmatic` TR-13.5: 三个预留接口已定义

## [ ] Task 14: FastAPI主应用和静态文件配置
- **Priority**: P0
- **Depends On**: Task 13
- **Description**: 
  - 创建FastAPI主应用
  - 配置Jinja2模板引擎
  - 配置静态文件服务
  - 注册所有API路由
  - 配置应用启动和关闭事件（调度器）
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `programmatic` TR-14.1: 应用能正常启动
  - `programmatic` TR-14.2: 静态文件能正常访问
  - `programmatic` TR-14.3: 所有API路由已注册

## [ ] Task 15: Web模板基础结构实现
- **Priority**: P1
- **Depends On**: Task 14
- **Description**: 
  - 创建base.html基础模板
  - 实现导航栏和页脚
  - 实现基础CSS样式
  - 实现基础JavaScript工具
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-15.1: 基础模板结构完整
  - `human-judgement` TR-15.2: 导航栏和页脚正常显示
  - `human-judgement` TR-15.3: 页面样式美观

## [ ] Task 16: 新闻列表页实现
- **Priority**: P1
- **Depends On**: Task 15
- **Description**: 
  - 创建index.html新闻列表页模板
  - 实现分页功能
  - 显示新闻标题、来源、发布时间、摘要
  - 按时间倒序排列
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-16.1: 新闻列表正常显示
  - `human-judgement` TR-16.2: 分页功能正常
  - `human-judgement` TR-16.3: 点击新闻能跳转到详情页

## [ ] Task 17: 新闻详情页实现
- **Priority**: P1
- **Depends On**: Task 16
- **Description**: 
  - 创建news_detail.html详情页模板
  - 展示完整新闻内容
  - 显示图片
  - 提供原文链接跳转
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-17.1: 新闻详情完整显示
  - `human-judgement` TR-17.2: 图片正常显示
  - `human-judgement` TR-17.3: 原文链接能正常跳转

## [ ] Task 18: 抓取控制页实现
- **Priority**: P1
- **Depends On**: Task 17
- **Description**: 
  - 创建crawl_control.html控制页模板
  - 实现手动触发抓取按钮
  - 显示抓取状态（进行中/完成）
  - 显示最近一次抓取时间
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-18.1: 抓取控制页正常显示
  - `human-judgement` TR-18.2: 点击按钮能触发抓取
  - `human-judgement` TR-18.3: 抓取状态正常更新

## [ ] Task 19: 筛选规则管理页实现
- **Priority**: P1
- **Depends On**: Task 18
- **Description**: 
  - 创建filter_rules.html规则管理页模板
  - 配置包含关键词输入
  - 配置排除关键词输入
  - 实现保存/重置规则功能
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-19.1: 规则管理页正常显示
  - `human-judgement` TR-19.2: 能保存和更新筛选规则
  - `human-judgement` TR-19.3: 重置功能正常

## [ ] Task 20: 抓取日志页实现
- **Priority**: P1
- **Depends On**: Task 19
- **Description**: 
  - 创建crawl_logs.html日志页模板
  - 显示抓取历史记录
  - 显示每次抓取的时间、数量、状态、错误信息
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `human-judgement` TR-20.1: 日志页正常显示
  - `human-judgement` TR-20.2: 抓取历史记录完整
  - `human-judgement` TR-20.3: 错误信息正常显示

## [ ] Task 21: 完整流程测试和优化
- **Priority**: P0
- **Depends On**: Task 20
- **Description**: 
  - 端到端完整流程测试
  - Bug修复
  - 性能优化
  - 用户体验优化
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `human-judgement` TR-21.1: 完整流程（抓取→查看→筛选→日志）正常
  - `programmatic` TR-21.2: 无明显Bug
  - `human-judgement` TR-21.3: 用户体验良好
