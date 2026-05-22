"""豆包大模型对话实现"""

import logging

import requests

from bot.analyzer_base import BaseChatAnalyzer

logger = logging.getLogger(__name__)


class DoubaoChat(BaseChatAnalyzer):
    """豆包大模型对话"""

    def __init__(
        self,
        api_key: str,
        model: str = "doubao-1-5-pro-32k-250115",
        region: str = "cn-beijing",
    ):
        self._api_key = api_key
        self._model = model
        self._region = region
        self._base_url = f"https://ark.{region}.volces.com/api/v3/chat/completions"

    def _call_api(self, user_message: str, system_prompt: str) -> str:
        """调用豆包 API 的统一入口"""
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
            "stream": False,
        }

        try:
            response = requests.post(
                self._base_url,
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("豆包 API 调用失败")
            return "Error: 豆包 API 调用失败"

    def chat(self, user_message: str, system_prompt: str) -> str:
        """通用对话接口"""
        logger.info("发起豆包对话请求")
        return self._call_api(user_message, system_prompt)