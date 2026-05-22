"""飞书机器人 WebSocket 长连接，监听私聊消息并用 AI 回复。"""

import json
import logging
import os

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from bot.analyzer_factory import AnalyzerFactory

logger = logging.getLogger(__name__)


def start():
    """启动飞书 WebSocket 长连接监听。"""
    app_id = os.getenv("BOT_FEISHU_APP_ID", "")
    app_secret = os.getenv("BOT_FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        logger.error("BOT_FEISHU_APP_ID 或 BOT_FEISHU_APP_SECRET 环境变量未设置，无法启动飞书机器人。")
        print("Error: BOT_FEISHU_APP_ID 或 BOT_FEISHU_APP_SECRET 环境变量未设置，无法启动飞书机器人。")
        return

    analyzer = AnalyzerFactory.create()

    system_prompt = "你是企业内部办公助手，回答正式简洁、逻辑稳妥，贴合职场沟通，不闲聊发散，务实解答工作各类问题。"

    def handle_message(event: P2ImMessageReceiveV1):
        msg = event.event.message
        if msg.message_type != "text":
            return

        user_text = json.loads(msg.content).get("text", "").strip()
        if not user_text:
            return

        logger.info("收到用户消息: %s", user_text)

        try:
            ai_reply = analyzer.chat(user_text, system_prompt)
        except Exception as e:
            logger.error("AI 回复生成失败: %s", e)
            ai_reply = f"AI 回复生成失败：{e}"

        logger.info("AI 回复: %s", ai_reply)

        try:
            client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
            client.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .receive_id(msg.chat_id)
                .msg_type("text")
                .content(json.dumps({"text": ai_reply}))
                .build()
            )
        except Exception as e:
            logger.error("发送飞书消息失败: %s", e)

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

    model_name = getattr(analyzer, "_model", type(analyzer).__name__)
    print(f"飞书机器人已启动，模型: {model_name}，监听私聊消息中...")

    ws_client.start()