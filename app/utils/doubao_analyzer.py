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
        """
        使用豆包大模型分析新闻
        输出：核心事实 + 事件背景 + 各方动机 + 潜在影响 + 趋势判断
        """
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
            resp = requests.post(self.url, headers=headers, json=data, timeout=30)

            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and len(result["choices"]) > 0:
                    logger.info(f"豆包分析成功: {news_title}")
                    return result["choices"][0]["message"]["content"]
                else:
                    logger.error(f"API 响应结构错误: {result}")
                    return None
            else:
                logger.error(f"分析失败 {resp.status_code}: {resp.text}")
                return None

        except Exception as e:
            logger.error(f"新闻分析出错: {str(e)}", exc_info=True)
            return None

    def send_to_feishu(self, news_title: str, analysis_result: str, source: str = "") -> bool:
        """
        将分析结果发送到飞书
        """
        if not self.feishu_webhook_url:
            logger.warning("飞书 webhook 未配置")
            return False

        content_lines = [
            f"【{self.keyword}】📰 新闻深度分析",
            f"来源: {source}" if source else "",
            f"标题: {news_title}",
            "",
            "===== 分析结果 =====",
            analysis_result
        ]

        content = "\n".join([line for line in content_lines if line])

        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        try:
            response = requests.post(self.feishu_webhook_url, json=payload, timeout=10)
            result = response.json()
            if result.get("code") == 0:
                logger.info(f"飞书推送成功: {news_title}")
                return True
            else:
                logger.warning(f"飞书推送失败: {result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"飞书推送异常: {e}", exc_info=True)
            return False

    def analyze_and_push(self, news_title: str, news_content: str, source: str = "") -> bool:
        """
        分析新闻并推送到飞书
        """
        logger.info(f"开始分析新闻: {news_title}")

        analysis_result = self.analyze_news(news_content, news_title)
        if not analysis_result:
            logger.error("新闻分析失败，跳过推送")
            return False

        return self.send_to_feishu(news_title, analysis_result, source)

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
