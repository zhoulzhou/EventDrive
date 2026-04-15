import logging
from typing import Optional
from volcenginesdkarkruntime import Ark

logger = logging.getLogger(__name__)


class KnowledgeAnalyzer:
    def __init__(
        self,
        ak: str,
        sk: str,
        model_id: str,
        region: str = "cn-beijing",
        feishu_webhook_url: str = "",
        keyword: str = "Talk"
    ):
        self.ak = ak
        self.sk = sk
        self.model_id = model_id
        self.region = region
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword

        self.client = Ark(ak=ak, sk=sk)

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        """
        使用豆包大模型分析新闻
        输出：核心事实 + 事件背景 + 各方动机 + 潜在影响 + 趋势判断
        """
        prompt = f"""你是资深国际新闻分析师，请严格按以下结构输出，不冗余、不编造、不跑偏。

输出必须包含5个部分，每个部分独立成段，不交叉、不混淆：
1. 核心事实（3句话以内，仅客观陈述，无主观判断）
2. 事件背景（1-2句话，梳理历史脉络与前提，贴合新闻本身）
3. 各方动机（明确核心相关方，分别说明利益诉求与出发点）
4. 潜在影响（分2-3点，说明对相关方、地区局势的直接/间接影响）
5. 趋势判断（1-2个合理预测，不绝对化、不臆测）

新闻标题：{news_title}
新闻内容：
{news_content}
"""

        try:
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                temperature=0.6
            )

            usage = response.usage
            logger.info(f"✅ 分析完成 | 输入token: {usage.prompt_tokens} | 输出token: {usage.completion_tokens} | 总消耗: {usage.total_tokens}")

            analysis_result = response.choices[0].message.content
            logger.info(f"新闻分析成功: {news_title}")
            return analysis_result
        except Exception as e:
            logger.error(f"新闻分析失败: {e}", exc_info=True)
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
            import requests
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


_analyzer: Optional[KnowledgeAnalyzer] = None


def init_knowledge_analyzer(
    api_key: str,
    kb_service_id: str,
    region: str = "cn-beijing",
    feishu_webhook_url: str = "",
    keyword: str = "Talk"
):
    global _analyzer
    logger.error("init_knowledge_analyzer 参数错误：现在需要使用 AK/SK 认证方式")
    logger.error("请使用 init_knowledge_analyzer_with_ak_sk() 函数")


def init_knowledge_analyzer_with_ak_sk(
    ak: str,
    sk: str,
    model_id: str,
    region: str = "cn-beijing",
    feishu_webhook_url: str = "",
    keyword: str = "Talk"
):
    global _analyzer
    _analyzer = KnowledgeAnalyzer(
        ak=ak,
        sk=sk,
        model_id=model_id,
        region=region,
        feishu_webhook_url=feishu_webhook_url,
        keyword=keyword
    )
    logger.info(f"豆包大模型分析器已初始化，模型ID: '{model_id}', 区域: '{region}'")


def get_knowledge_analyzer() -> Optional[KnowledgeAnalyzer]:
    return _analyzer
