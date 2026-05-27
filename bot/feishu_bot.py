"""飞书机器人 WebSocket 长连接，监听私聊消息并用 AI 回复。"""

import json
import logging
import os
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from bot.analyzer_factory import AnalyzerFactory

logger = logging.getLogger(__name__)


# 已处理的消息 ID 集合，防止 WebSocket 重连重复投递
_processed_msg_ids: set = set()

# 定时清理旧 ID，防止内存泄漏
import time as _time

_last_cleanup = _time.time()
_CLEANUP_INTERVAL = 300


def _is_duplicate(msg_id: str) -> bool:
    """检查消息是否已处理过，并定期清理旧记录"""
    global _last_cleanup
    if _time.time() - _last_cleanup > _CLEANUP_INTERVAL:
        _processed_msg_ids.clear()
        _last_cleanup = _time.time()
    if msg_id in _processed_msg_ids:
        return True
    _processed_msg_ids.add(msg_id)
    return False


def _log(msg: str):
    """输出日志（logging 已配置输出到 stderr，终端实时可见）"""
    print(msg, file=sys.stderr, flush=True)


def start():
    """启动飞书 WebSocket 长连接监听。"""
    app_id = os.getenv("BOT_FEISHU_APP_ID", "")
    app_secret = os.getenv("BOT_FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        msg = "BOT_FEISHU_APP_ID 或 BOT_FEISHU_APP_SECRET 未设置，无法启动"
        logger.error(msg)
        print(f"Error: {msg}", file=sys.stderr)
        return

    _log(f"飞书 App ID: {app_id[:16]}...")
    _log("正在创建 AI 分析器...")

    analyzer = AnalyzerFactory.create()
    _log(f"AI 分析器已创建: {type(analyzer).__name__}")

    system_prompt = "你是全能知识助手，精通全领域知识。作答准确客观，擅长逻辑拆解、归纳总结与对比辨析。复杂内容优先分点排版，语言自然得体，通俗易理解。聚焦问题本身，不闲聊、不编造，不确定内容如实说明，输出务实有价值的信息。"

    def handle_message(event):
        # event: lark_oapi.api.im.v1.P2ImMessageReceiveV1

        msg = event.event.message

        # 去重：防止 WebSocket 重连时重复投递同一事件
        msg_id = msg.message_id
        if _is_duplicate(msg_id):
            _log(f">>> 重复消息已跳过: {msg_id} <<<")
            return

        _log(f">>> 收到飞书消息 <<< msg_id={msg_id}")
        _log(f"消息类型: {msg.message_type}, chat_id: {msg.chat_id}")

        if msg.message_type != "text":
            _log(f"非文本消息({msg.message_type})，忽略")
            return

        try:
            content_data = json.loads(msg.content)
            user_text = content_data.get("text", "").strip()
        except json.JSONDecodeError as e:
            _log(f"消息内容 JSON 解析失败: {e}, content={msg.content[:200]}")
            return

        if not user_text:
            _log("消息文本为空，忽略")
            return

        _log(f"用户消息原文: {user_text[:200]}")
 
        try:
            _log("正在调用 AI...")
            ai_reply = analyzer.chat(user_text, system_prompt)
            _log(f"AI 回复: {ai_reply[:200]}")
        except Exception as e:
            logger.exception("AI 调用异常")
            ai_reply = f"AI 回复生成失败：{e}"
            _log(f"AI 异常: {e}")

        try:
            _log("正在发送回复到飞书...")
            client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            resp = client.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": ai_reply}))
                    .build()
                )
                .build()
            )
            _log(f"消息发送成功, msg_id={resp.data.message_id if resp.data else 'N/A'}")
        except Exception as e:
            logger.exception("发送消息异常")
            _log(f"发送消息失败: {e}")

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.WARNING,
    )

    print(f"飞书机器人已启动，模型: {type(analyzer).__name__}，监听私聊消息中...")
    print("WebSocket 长连接建立中...", flush=True)
    _log("WebSocket 长连接建立中...")

    ws_client.start()