"""分析器工厂 - 根据 env 配置创建对应的大模型实例"""

import os
from bot.analyzer_base import BaseChatAnalyzer


class AnalyzerFactory:
    """根据 BOT_AI_MODEL env 配置创建分析器"""

    @staticmethod
    def create() -> BaseChatAnalyzer:
        model_type = os.getenv("BOT_AI_MODEL", "deepseek").strip().lower()

        if model_type == "deepseek":
            from bot.deepseek_chat import DeepSeekChat
            api_key = os.getenv("BOT_DEEPSEEK_API_KEY", "")
            if not api_key:
                raise ValueError("BOT_DEEPSEEK_API_KEY 未配置，请在 .env 中设置")
            model = os.getenv("BOT_DEEPSEEK_MODEL", "deepseek-chat")
            return DeepSeekChat(api_key=api_key, model=model)

        elif model_type == "doubao":
            from bot.doubao_chat import DoubaoChat
            api_key = os.getenv("BOT_DOUBAO_API_KEY", "")
            if not api_key:
                raise ValueError("BOT_DOUBAO_API_KEY 未配置，请在 .env 中设置")
            model = os.getenv("BOT_DOUBAO_MODEL", "doubao-1-5-pro-32k-250115")
            region = os.getenv("BOT_DOUBAO_REGION", "cn-beijing")
            return DoubaoChat(api_key=api_key, model=model, region=region)

        elif model_type == "openrouter":
            from bot.openrouter_chat import OpenRouterChat
            api_key = os.getenv("BOT_OPENROUTER_API_KEY", "")
            if not api_key:
                raise ValueError("BOT_OPENROUTER_API_KEY 未配置，请在 .env 中设置")
            return OpenRouterChat(api_key=api_key)

        else:
            raise ValueError(f"不支持的模型类型: '{model_type}'，可选: deepseek / doubao / openrouter")