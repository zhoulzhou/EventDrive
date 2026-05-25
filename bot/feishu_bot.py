"""飞书机器人 WebSocket 长连接，监听私聊消息并用 AI 回复。"""

import json
import logging
import os
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from bot.analyzer_factory import AnalyzerFactory

logger = logging.getLogger(__name__)


def _log(msg: str):
    """同时输出到 logger 和 stderr（确保终端和日志文件都能看到）"""
    logger.info(msg)
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

    system_prompt = "你是企业内部办公助手，回答正式简洁、逻辑稳妥，贴合职场沟通，不闲聊发散，务实解答工作各类问题。"

    def handle_message(event: P2ImMessageReceiveV1):
        _log(">>> 收到飞书消息事件 <<<")

        msg = event.event.message
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
                .receive_id(msg.chat_id)
                .msg_type("text")
                .content(json.dumps({"text": ai_reply}))
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