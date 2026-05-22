# 飞书互动助手 - Verification Checklist

## 代码结构
- [x] `bot/` 目录独立于 `app/`，无任何 `from app...` 导入
- [x] `bot/__init__.py` 存在
- [x] `bot/analyzer_base.py` 存在，`BaseChatAnalyzer` 定义 `chat()` 抽象方法
- [x] `bot/analyzer_factory.py` 存在，`AnalyzerFactory.create()` 通过 env 切换模型
- [x] `bot/deepseek_chat.py`、`bot/doubao_chat.py`、`bot/openrouter_chat.py` 各自实现 `chat()`
- [x] `bot/feishu_bot.py` 存在，基于 `lark.ws.Client` WebSocket 长连接
- [x] `bot/runner.py` 存在，启动入口
- [x] `run_bot.py` 存在（项目根目录）
- [x] 无 `summarize()`、`BOT_CRAWL_INTERVAL`、`BOT_FEISHU_WEBHOOK_URL` 残留引用

## 模型抽象层
- [x] `BaseChatAnalyzer` 使用 `abc.ABC` + `@abstractmethod`
- [x] 三个模型类均实现 `chat()`
- [x] API 异常返回错误字符串，不抛异常
- [x] `AnalyzerFactory.create()` 根据 `BOT_AI_MODEL` 返回正确实例

## 飞书机器人
- [x] WebSocket 长连接
- [x] 仅处理 `message_type == "text"` 消息
- [x] 调用 `chat()` 获取 AI 回复并发送回原会话
- [x] 异常不中断长连接

## 配置
- [x] `.env` 包含所有 `BOT_*` 配置项，均为独立字段不复用新闻系统密钥
- [x] `.env.example` 包含 `BOT_*` 配置示例及中文注释
- [x] `requirements.txt` 包含 `lark-oapi`

## 集成验证
- [x] 所有模块 import 无报错
- [x] `python run_bot.py` 可启动