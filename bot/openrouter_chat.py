"""OpenRouter 对话实现"""

import logging

from openai import OpenAI

from bot.analyzer_base import BaseChatAnalyzer

logger = logging.getLogger(__name__)


class OpenRouterChat(BaseChatAnalyzer):
    """通过 OpenRouter 调用大模型的对话实现"""

    def __init__(self, api_key: str):
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )

    def chat(self, user_message: str, system_prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model="openrouter/auto",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                stream=False,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error("OpenRouter chat 调用失败: %s", e)
            return f"对话出错: {e}"