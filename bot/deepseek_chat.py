"""DeepSeek 大模型对话实现

支持:
  - Token 用量统计 + 成本估算
  - 多轮对话历史（自动缓存命中，省钱）
  - 超时/HTTP/通用异常分类处理
"""

import logging

import requests

from bot.analyzer_base import BaseChatAnalyzer

logger = logging.getLogger(__name__)

# DeepSeek V4-Pro 定价（元/百万 tokens）
_PRICE_INPUT = 3.0    # 输入（未命中缓存）
_PRICE_OUTPUT = 6.0   # 输出
_PRICE_INPUT_CACHE = 0.75  # 输入（命中缓存）


class DeepSeekChat(BaseChatAnalyzer):
    """DeepSeek Chat API，支持多轮对话 + 成本统计"""

    def __init__(self, api_key: str, model: str = "deepseek-v4-pro"):
        self._api_key = api_key
        self._model = model
        self._base_url = "https://api.deepseek.com/v1/chat/completions"

    def chat(self, user_message: str, system_prompt: str) -> str:
        """单轮对话（兼容 BaseChatAnalyzer 接口）"""
        return self._call_api(user_message, system_prompt=system_prompt)

    def chat_with_history(
        self,
        user_message: str,
        system_prompt: str = "",
        history: list | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """多轮对话（带历史上下文，自动缓存命中）"""
        return self._call_api(
            user_message,
            system_prompt=system_prompt,
            history_messages=history,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _call_api(
        self,
        user_message: str,
        system_prompt: str = "",
        history_messages: list | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # 构建消息上下文
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 追加历史对话（多轮 = 自动缓存命中）
        if history_messages and isinstance(history_messages, list):
            messages.extend(history_messages)

        # 追加当前用户问题
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        try:
            logger.info("【DeepSeek】调用 API model=%s", self._model)
            resp = requests.post(
                self._base_url,
                headers=headers,
                json=payload,
                timeout=60,
                proxies={"http": None, "https": None},
            )
            resp.raise_for_status()
            data = resp.json()

            content = data["choices"][0]["message"]["content"]

            # Token 用量 + 成本
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            cache_hit = usage.get("prompt_cache_hit_tokens", 0)
            cache_miss = usage.get("prompt_cache_miss_tokens", prompt_tokens - cache_hit)

            cost_cached = cache_hit * _PRICE_INPUT_CACHE / 1_000_000
            cost_uncached = cache_miss * _PRICE_INPUT / 1_000_000
            cost_output = completion_tokens * _PRICE_OUTPUT / 1_000_000
            total_cost = cost_cached + cost_uncached + cost_output

            logger.info(
                "【DeepSeek】成功 | 输入=%d(命中缓存%d) 输出=%d | 预估成本 ¥%.4f",
                prompt_tokens, cache_hit, completion_tokens, total_cost,
            )
            return content.strip()

        except requests.exceptions.Timeout:
            logger.error("【DeepSeek】超时")
            return "AI 应答超时，请稍后重试"
        except requests.exceptions.HTTPError as e:
            logger.error("【DeepSeek】HTTP错误 %s | %s", resp.status_code, resp.text[:200])
            return f"AI服务异常：HTTP错误 {resp.status_code}"
        except Exception as e:
            logger.error("【DeepSeek】异常: %s", e)
            return f"AI应答异常：{e}"