import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class OpenRouterAnalyzer:
    def __init__(
        self,
        api_key: str,
        feishu_webhook_url: str = "",
        keyword: str = "Talk"
    ):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = "deepseek-chat-v3"
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        prompt = f"""You are a professional US stock market analyst. Based on the news below, analyze the impact on US listed companies.

【News Title】
{news_title}

【News Summary】
{news_content}

Structure your answer strictly in 4 parts, concise and investment-ready:

1. Overall Impact
- Sentiment: Positive / Negative / Neutral
- Magnitude: Strong / Moderate / Weak

2. Benefiting US Companies & Logic
List company names + core reasons (revenue, profit, demand, regulation, cost, etc.)

3. Hurting US Companies & Logic
List company names + core negative impacts

4. Investment Recommendations
Clear action for each related stock: Buy / Add / Hold / Reduce / Sell / Avoid
"""

        try:
            session = requests.Session()
            session.trust_env = False

            resp = session.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                    "stream": False
                },
                timeout=45
            )

            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and len(result["choices"]) > 0:
                    logger.info(f"新闻分析成功: {news_title}")
                    return result["choices"][0]["message"]["content"]
                else:
                    logger.error(f"API 响应结构错误: {result}")
                    return None
            else:
                logger.error(f"分析失败 {resp.status_code}: {resp.text[:100]}")
                return None

        except Exception as e:
            logger.error(f"新闻分析出错: {str(e)}", exc_info=True)
            return None

    def analyze_only(self, news_title: str, news_content: str, source: str = "") -> Optional[str]:
        logger.info(f"开始分析新闻: {news_title}")
        analysis_result = self.analyze_news(news_content, news_title)
        return analysis_result


_analyzer: Optional[OpenRouterAnalyzer] = None


def init_openrouter_analyzer(
    api_key: str,
    feishu_webhook_url: str = "",
    keyword: str = "Talk"
):
    global _analyzer
    _analyzer = OpenRouterAnalyzer(
        api_key=api_key,
        feishu_webhook_url=feishu_webhook_url,
        keyword=keyword
    )
    logger.info(f"OpenRouter 大模型分析器已初始化，模型: {_analyzer.model if _analyzer else 'unknown'}")


def get_openrouter_analyzer() -> Optional[OpenRouterAnalyzer]:
    return _analyzer