import logging
from typing import Optional
from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenRouterAnalyzer:
    def __init__(
        self,
        api_key: str,
        feishu_webhook_url: str = "",
        keyword: str = "Talk"
    ):
        self.api_key = api_key
        self.model = "openrouter/free"
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword
        self.last_used_model = ""
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key
        )

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        prompt = f"""You are a professional financial news analyst. Provide a deep, structured analysis of the following news, strictly following the 4 dimensions below:

【News Title】
{news_title}

【News Summary】
{news_content}

【Analysis Requirements】
1. Core Event
   - Summarize the core facts in one sentence
   - Identify the time, key entities, and critical data

2. Key Impact
   - Impact on macro economy / policy
   - Impact on related industries
   - Impact on capital markets (stocks, bonds, forex, etc.)

3. Market Sentiment
   - Overall sentiment: Optimistic / Neutral / Pessimistic
   - Driving factors behind the sentiment
   - Comparison with similar historical events

4. Risk Warning
   - List 2-3 key risks to watch
   - Trigger conditions for each risk
   - Recommended response strategies

【Output Format】Strictly follow this structure, no extra text
### 1. Core Event
- Summary:
- Key Entities:
- Critical Data:

### 2. Key Impact
- Macro Impact:
- Industry Impact:
- Capital Market Impact:

### 3. Market Sentiment
- Overall Sentiment:
- Driving Factors:
- Historical Reference:

### 4. Risk Warning
1. Risk 1:
   - Trigger Condition:
   - Response Strategy:
2. Risk 2:
   - Trigger Condition:
   - Response Strategy:
3. Risk 3:
   - Trigger Condition:
   - Response Strategy:
"""

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                stream=False
            )

            self.last_used_model = resp.model or ""
            logger.info(f"OpenRouter分析成功: {news_title} (实际模型: {self.last_used_model})")

            if resp.choices and len(resp.choices) > 0:
                return resp.choices[0].message.content
            else:
                logger.error(f"API 响应结构错误: 无 choices")
                return None

        except Exception as e:
            logger.error(f"OpenRouter分析出错: {str(e)}", exc_info=True)
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
    logger.info(f"OpenRouter 大模型分析器已初始化，使用免费模型自动路由")


def get_openrouter_analyzer() -> Optional[OpenRouterAnalyzer]:
    return _analyzer