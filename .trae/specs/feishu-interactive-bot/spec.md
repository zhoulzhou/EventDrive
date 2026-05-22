# 飞书互动助手 Spec

## Why
需要一个独立于新闻系统的飞书互动助手，用户在飞书私聊中发消息，AI 大模型实时回复。大模型通过抽象层切换，所有配置走 env 文件。与 `app/` 完全解耦。

## What Changes
- **新增** `bot/` 顶层目录，独立于 `app/`
- **新增** 模型抽象基类 `bot/analyzer_base.py`，定义 `chat()` 对话接口
- **新增** `bot/analyzer_factory.py`，通过 env 配置选择大模型
- **新增** `bot/deepseek_chat.py`、`bot/doubao_chat.py`、`bot/openrouter_chat.py`，各自实现 `chat()`
- **新增** `bot/feishu_bot.py`，飞书 WebSocket 长连接监听私聊消息并调用模型回复
- **新增** `bot/runner.py` 启动入口
- **新增** `run_bot.py` 独立启动脚本
- 新增 env 配置项（全部 `BOT_*` 前缀）
- **不修改** `app/` 目录下任何文件

## Impact
- Affected specs: 无
- Affected code: 仅新增文件
  - 新增: `bot/__init__.py`, `bot/analyzer_base.py`, `bot/analyzer_factory.py`, `bot/deepseek_chat.py`, `bot/doubao_chat.py`, `bot/openrouter_chat.py`, `bot/feishu_bot.py`, `bot/runner.py`
  - 新增: `run_bot.py`
  - 修改: `.env.example`, `requirements.txt`

## ADDED Requirements

### Requirement: 飞书长连接交互机器人
系统 SHALL 基于飞书 WebSocket 长连接监听私聊消息，用户发消息直接调用大模型实时回复。

#### Scenario: 私聊消息自动回复
- **WHEN** 用户在飞书中向机器人发送文本消息
- **THEN** 系统调用当前选定模型的 `chat()` 方法获取回复并发送回原会话

#### Scenario: 非文本消息忽略
- **WHEN** 收到非文本消息
- **THEN** 系统忽略不处理

### Requirement: 模型抽象层
系统 SHALL 在 `bot/` 目录下提供大模型对话抽象层，所有模型实现统一 `chat()` 接口，通过 env 配置切换。

#### Scenario: 统一接口
- **WHEN** 开发者创建新的模型类
- **THEN** 该类必须继承 `BaseChatAnalyzer` 并实现 `chat(user_message, system_prompt) -> str`

#### Scenario: 配置切换模型
- **WHEN** 修改 `.env` 中 `BOT_AI_MODEL=doubao` 并重启 bot
- **THEN** 飞书对话回复切换到豆包模型，无需修改代码

### Requirement: 配置全部走 env
系统 SHALL 将所有配置项放在 `.env` 文件中，以 `BOT_` 为前缀，区别于新闻系统配置。