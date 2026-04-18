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

    def analyze_news(self, news_content: str, news_title: str = "", use_english: bool = False) -> Optional[str]:
        if use_english:
            prompt = f"""You are a professional institutional equity market analyst. Analyze the following news deeply and structurally for US stock market investment decisions. Follow the 4 dimensions strictly. If unable to analyze, reply "Unable to analyze". Do not skip, do not generalize, do not use hypothetical news.

【News Title】
{news_title}

【News Summary】
{news_content}

【Analysis Requirements】

1. Impact on Macro Investment Environment
   - Impact Types: Policy/Economy/Liquidity/Geopolitical/Sentiment
   - Direction: Positive/Negative
   - Intensity: Strong/Medium/Weak
   - Logic: Explain how it changes risk appetite, capital flows, and overall market valuation

2. Impact on Overall Stock Market
   - Affected Indices: US Stocks (S&P 500/Nasdaq/DJIA)/Tech Stocks/Cyclical Stocks/etc.
   - Direction: Bullish/Bearish
   - Impact Range: Global/Sector-specific
   - Mechanism: Affects valuation, earnings expectations, risk premium

3. Impact on Related Companies (List by points)
   - Benefiting Companies: Name + Benefit Logic (Revenue/Profit/Market Share/Policy)
   - Damaged Companies: Name + Damage Logic
   - Impact Degree: Long-term/Short-term/One-time

4. Investment & Trading Suggestions (Clear, Executable)
   - Overall Position Suggestion: Add/Hold/Reduce/Watch
   - Sector Allocation: Overweight/Neutral/Underweight
   - Stock Suggestions:
     - Benefiting stocks: Buy/Add/Hold
     - Damaged stocks: Sell/Reduce/Avoid
   - Risk Warnings: Must list 2-3 key risks

【Output Format】Strictly follow this structure, no extra text

### 1. Macro Investment Environment Impact
- Impact Type:
- Impact Direction:
- Impact Intensity:
- Core Logic:

### 2. Overall Stock Market Impact
- Affected Markets:
- Impact Direction:
- Impact Range:
- Transmission Mechanism:

### 3. Related Companies Impact
#### Benefiting Companies (Positive)
1. Company: XXX
   - Logic:
   - Period: Short-term/Long-term
2. ...

#### Damaged Companies (Negative)
1. Company: XXX
   - Logic:
   - Period: Short-term/Long-term

### 4. Investment Operation Suggestions (Clear & Executable)
- Overall Position:
- Sector Allocation:
- Stock Operations:
  - Buy/Add:
  - Sell/Reduce:
  - Hold/Watch:
- Key Risk Warnings:
"""
        else:
            prompt = f"""你是专业的机构级股票市场分析师。请对以下新闻做深度、结构化、可投资决策的分析，**严格按4个维度输出**，无法分析时直接回复"无法分析"，不许省略、不许泛泛而谈、不许用假设新闻分析：

【新闻原文】
{news_content}

【分析要求】
1. 对宏观投资环境的影响
   - 影响大类：政策/经济/流动性/地缘/情绪
   - 方向：正面/负面
   - 强度：强/中/弱
   - 逻辑：说明如何改变风险偏好，资金流向、市场整体估值

2. 对整体股票市场的影响
   - 影响指数：A股/美股/港股/科技股/周期股等
   - 方向：利好/利空
   - 影响范围：全局/局部/行业性
   - 机制：影响估值、盈利预期、风险溢价

3. 对相关上市公司的影响（分点列出）
   - 受益公司：名单 + 受益逻辑（营收/利润/市占率/政策）
   - 受损公司：名单 + 受损逻辑
   - 影响程度：长期/短期/一次性

4. 投资与买卖建议（明确、可执行）
   - 整体仓位建议：加仓/减仓/持有/观望
   - 行业配置建议：超配/标配/低配
   - 个股建议（针对上面受益/受损）：
     - 受益股：买入/增持/持有
     - 受损股：卖出/减仓/回避
   - 风险提示：必须列2–3条关键风险

【输出格式】严格用以下结构，不要多余文字
### 1. 对宏观投资环境的影响
- 影响类型：
- 影响方向：
- 影响强度：
- 核心逻辑：

### 2. 对整体股票市场的影响
- 影响市场：
- 影响方向：
- 影响范围：
- 传导机制：

### 3. 对相关上市公司的影响
#### 受益公司（正面）
1. 公司：XXX
   - 逻辑：
   - 周期：短期/长期
2. ...

#### 受损公司（负面）
1. 公司：XXX
   - 逻辑：
   - 周期：短期/长期

### 4. 投资操作建议（明确可执行）
- 整体仓位：
- 行业配置：
- 个股操作：
  - 买入/增持：
  - 卖出/减仓：
  - 持有/观望：
- 关键风险提示：
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

    def analyze_only(self, news_title: str, news_content: str, source: str = "", use_english: bool = False) -> Optional[str]:
        logger.info(f"开始分析新闻: {news_title}, use_english={use_english}")
        analysis_result = self.analyze_news(news_content, news_title, use_english)
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