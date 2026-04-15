import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class KnowledgeAnalyzer:
    def __init__(
        self,
        ak: str,
        sk: str,
        feishu_webhook_url: str = "",
        keyword: str = "Talk"
    ):
        self.ak = ak
        self.sk = sk
        self.model = "doubao-1-5-pro-32k-250115"
        self.url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        """
        使用豆包大模型分析新闻（原生requests方式）
        输出：核心事实 + 事件背景 + 各方动机 + 潜在影响 + 趋势判断
        """
        prompt = f"""你是专业新闻分析师，严格按以下5点输出，不废话、不编造、不跑偏：

1. 核心事实（3句话内）
2. 事件背景
3. 各方动机
4. 潜在影响
5. 趋势判断

新闻标题：{news_title}
新闻内容：
{news_content}
"""

        try:
            headers = {
                "Content-Type": "application/json"
            }

            data = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.6
            }

            auth = (self.ak, self.sk)

            response = requests.post(
                self.url,
                headers=headers,
                json=data,
                auth=auth,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    logger.info(f"新闻分析成功: {news_title}")
                    return result["choices"][0]["message"]["content"]
                else:
                    logger.error(f"API 响应结构错误: {result}")
                    return None
            else:
                logger.error(f"分析失败 {response.status_code}: {response.text}")
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
    model_id: str = "doubao-1-5-pro-32k-250115",
    region: str = "cn-beijing",
    feishu_webhook_url: str = "",
    keyword: str = "Talk"
):
    global _analyzer
    _analyzer = KnowledgeAnalyzer(
        ak=ak,
        sk=sk,
        feishu_webhook_url=feishu_webhook_url,
        keyword=keyword
    )
    logger.info(f"豆包大模型分析器已初始化，模型: '{model_id}', 区域: '{region}'")


def get_knowledge_analyzer() -> Optional[KnowledgeAnalyzer]:
    return _analyzer
