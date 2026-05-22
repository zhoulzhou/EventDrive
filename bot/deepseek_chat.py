"""DeepSeek 大模型对话实现"""

import logging

import requests

from bot.analyzer_base import BaseChatAnalyzer

logger = logging.getLogger(__name__)


class DeepSeekChat(BaseChatAnalyzer):
    """DeepSeek Chat API 对话实现"""

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self._api_key = api_key
        self._model = model

    def chat(self, user_message: str, system_prompt: str) -> str:
        return self._call_api(user_message, system_prompt)

    def _call_api(self, user_message: str, system_prompt: str) -> str:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": False,
        }

        try:
            logger.info("调用 DeepSeek API，model=%s", self._model)
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=30,
                proxies={"http": None, "https": None},
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            logger.info("DeepSeek API 调用成功")
            return content
        except Exception as e:
            logger.error("DeepSeek API 调用异常: %s", e)
            return f"AI应答异常：{e}"