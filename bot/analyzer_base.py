"""大模型对话抽象基类"""

from abc import ABC, abstractmethod


class BaseChatAnalyzer(ABC):
    """所有大模型对话实现必须继承此类"""

    @abstractmethod
    def chat(self, user_message: str, system_prompt: str) -> str:
        """
        通用对话接口

        Args:
            user_message: 用户消息
            system_prompt: 系统提示词（人设）

        Returns:
            AI 回复文本，异常时返回错误提示字符串
        """
        ...