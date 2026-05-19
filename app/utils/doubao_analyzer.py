import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class DoubaoAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = "doubao-1-5-pro-32k-250115",
        region: str = "cn-beijing",
        feishu_webhook_url: str = "",
        keyword: str = "豆包"
    ):
        self.api_key = api_key
        self.model = model
        self.region = region
        self.url = f"https://ark.{region}.volces.com/api/v3/chat/completions"
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        prompt = f"""你是专业金融新闻分析师，请对以下新闻进行深度、结构化分析，严格按以下4个维度输出：

【新闻标题】
{news_title}

【新闻摘要】
{news_content}

【分析要求】
1. 核心事件
   - 用一句话概括新闻的核心事实
   - 明确事件发生的时间、主体、关键数据

2. 关键影响
   - 对宏观经济/政策的影响
   - 对相关行业的影响
   - 对资本市场（股市、债市、汇市等）的影响

3. 市场情绪
   - 判断市场整体情绪：乐观/中性/悲观
   - 分析情绪背后的驱动因素
   - 对比历史类似事件的市场反应

4. 风险提示
   - 列出2-3条需要警惕的关键风险
   - 说明每项风险可能触发的条件
   - 给出风险应对建议

【输出格式】严格用以下结构，不要多余文字
### 1. 核心事件
- 事件概括：
- 关键主体：
- 核心数据：

### 2. 关键影响
- 宏观影响：
- 行业影响：
- 资本市场影响：

### 3. 市场情绪
- 整体情绪：
- 驱动因素：
- 历史参考：

### 4. 风险提示
1. 风险一：
   - 触发条件：
   - 应对建议：
2. 风险二：
   - 触发条件：
   - 应对建议：
3. 风险三：
   - 触发条件：
   - 应对建议：
"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        data = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "stream": False
        }

        try:
            resp = requests.post(self.url, headers=headers, json=data, timeout=180)

            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and len(result["choices"]) > 0:
                    logger.info(f"豆包分析成功: {news_title}")
                    return result["choices"][0]["message"]["content"]
                else:
                    logger.error(f"API 响应结构错误, model={self.model}: {result}")
                    return None
            else:
                logger.error(f"豆包分析失败, model={self.model}, status={resp.status_code}: {resp.text[:500]}")
                return None

        except Exception as e:
            logger.error(f"新闻分析出错, model={self.model}: {str(e)}", exc_info=True)
            return None

    def analyze_only(self, news_title: str, news_content: str, source: str = "") -> Optional[str]:
        """
        只分析新闻，不推送到飞书，返回分析结果
        """
        logger.info(f"开始分析新闻: {news_title}")

        analysis_result = self.analyze_news(news_content, news_title)
        return analysis_result


_doubao_analyzer: Optional[DoubaoAnalyzer] = None


def init_doubao_analyzer(
    api_key: str,
    model: str = "doubao-1-5-pro-32k-250115",
    region: str = "cn-beijing",
    feishu_webhook_url: str = "",
    keyword: str = "豆包"
):
    global _doubao_analyzer
    _doubao_analyzer = DoubaoAnalyzer(
        api_key=api_key,
        model=model,
        region=region,
        feishu_webhook_url=feishu_webhook_url,
        keyword=keyword
    )
    logger.info(f"豆包大模型分析器已初始化，模型: '{model}', 区域: '{region}'")


def get_doubao_analyzer() -> Optional[DoubaoAnalyzer]:
    return _doubao_analyzer
