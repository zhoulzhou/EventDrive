# 新闻抓取应用 - Product Requirement Document

## Overview
- **Summary**: 一个基于Python的新闻抓取应用，支持从5大权威网站（财联社、新华网、东方财富、36氪、巨潮资讯）自动抓取新闻，通过Web界面查看和管理，支持自定义关键词筛选，并为飞书推送、AI打分和投资决策预留接口。
- **Purpose**: 解决从多个新闻源手动收集信息的效率问题，自动化抓取并筛选相关新闻，为后续的AI分析和投资决策提供数据基础。
- **Target Users**: 个人投资者、财经分析师、新闻资讯爱好者

## Goals
- 实现从5个指定网站的新闻抓取功能
- 提供Web界面进行新闻浏览、抓取控制和规则配置
- 支持定时和手动两种抓取方式
- 实现自定义关键词筛选功能
- 为飞书推送、AI打分和投资决策预留扩展接口

## Non-Goals (Out of Scope)
- 用户认证和权限系统（第一阶段）
- 新闻分类和标签系统（第一阶段）
- 数据统计和可视化（第一阶段）
- 飞书推送功能实现（第一阶段）
- AI打分功能实现（第一阶段）
- 投资决策功能实现（第一阶段）

## Background & Context
- 用户希望一个轻量级的解决方案，使用Python + FastAPI + SQLite
- 需要考虑反爬虫机制，使用Playwright进行浏览器自动化
- 单机运行，无需复杂的服务器部署

## Functional Requirements
- **FR-1**: 从5个指定网站抓取新闻（财联社、新华网、东方财富、36氪、巨潮资讯）
- **FR-2**: 每6小时定时抓取，支持手动触发抓取
- **FR-3**: 抓取最近24小时内的新闻，每个网站最多10条
- **FR-4**: 根据原文链接去重，避免重复保存
- **FR-5**: 下载并保存新闻图片到本地
- **FR-6**: Web界面显示新闻列表（分页）
- **FR-7**: Web界面显示新闻详情页
- **FR-8**: Web界面提供抓取控制功能
- **FR-9**: Web界面提供筛选规则管理（包含/排除关键词）
- **FR-10**: Web界面显示抓取历史日志
- **FR-11**: 实现关键词筛选功能（匹配标题+正文，OR逻辑）
- **FR-12**: 为飞书推送、AI打分和投资决策预留API接口

## Non-Functional Requirements
- **NFR-1**: 每次抓取请求之间有2-5秒随机延时
- **NFR-2**: 使用Playwright应对动态加载页面
- **NFR-3**: 请求头进行伪装
- **NFR-4**: Web界面响应及时，用户体验良好

## Constraints
- **Technical**: Python 3.8+, FastAPI, SQLite, Playwright, APScheduler
- **Business**: 单机运行，本地浏览器访问
- **Dependencies**: Playwright浏览器驱动, FastAPI依赖库

## Assumptions
- 目标网站在抓取期间结构不会发生重大变化
- 有足够的本地存储空间保存新闻和图片
- Playwright能正常安装和运行

## Acceptance Criteria

### AC-1: 项目初始化完成
- **Given**: 全新的项目目录
- **When**: 执行项目初始化
- **Then**: 生成完整的项目目录结构，包含requirements.txt、配置文件等
- **Verification**: `programmatic`

### AC-2: 数据库和模型建立
- **Given**: 项目已初始化
- **When**: 创建数据库模型和表
- **Then**: 成功创建news、filter_rules、crawl_logs三张表
- **Verification**: `programmatic`

### AC-3: 爬虫基类实现
- **Given**: 数据库已建立
- **When**: 实现爬虫基类
- **Then**: 基类包含请求头伪装、随机延时、Playwright集成等反爬虫功能
- **Verification**: `programmatic`

### AC-4: 5个网站爬虫实现
- **Given**: 爬虫基类已实现
- **When**: 实现每个网站的爬虫
- **Then**: 每个爬虫能成功抓取新闻并解析所需字段
- **Verification**: `programmatic`

### AC-5: 定时任务实现
- **Given**: 爬虫已实现
- **When**: 配置APScheduler
- **Then**: 每6小时自动触发一次抓取任务
- **Verification**: `programmatic`

### AC-6: FastAPI后端API实现
- **Given**: 数据库和爬虫已就绪
- **When**: 实现所有API端点
- **Then**: 提供新闻列表、详情、抓取控制、筛选规则、日志等API
- **Verification**: `programmatic`

### AC-7: Web前端界面实现
- **Given**: 后端API已就绪
- **When**: 实现Jinja2模板和前端页面
- **Then**: 提供新闻列表、详情、抓取控制、筛选规则、日志5个页面
- **Verification**: `human-judgment`

### AC-8: 筛选功能实现
- **Given**: 新闻数据已存在
- **When**: 配置筛选规则并查询
- **Then**: 正确返回符合包含关键词且不符合排除关键词的新闻
- **Verification**: `programmatic`

### AC-9: 预留接口设计
- **Given**: 基础功能已实现
- **When**: 设计预留接口
- **Then**: 飞书推送、AI打分、投资决策三个接口已设计完成
- **Verification**: `programmatic`

### AC-10: 完整流程验证
- **Given**: 所有功能已实现
- **When**: 运行完整流程（手动触发抓取→查看列表→查看详情→配置筛选→查看日志）
- **Then**: 所有功能正常工作
- **Verification**: `human-judgment`

## Open Questions
- 无
