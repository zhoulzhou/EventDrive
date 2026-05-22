"""飞书互动助手 启动入口

主线程：飞书 WebSocket 长连接，监听私聊消息 + AI 实时回复
"""

import sys
import logging

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)


def run():
    """启动飞书互动助手"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s - %(levelname)s - %(message)s"
    )

    from bot.feishu_bot import start as feishu_start

    print("飞书AI互动机器人已启动，可直接发消息对话")
    try:
        feishu_start()
    except KeyboardInterrupt:
        print("\n收到退出信号，正在关闭...")
        sys.exit(0)