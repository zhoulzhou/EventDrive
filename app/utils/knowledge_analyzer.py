import json
import requests
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class KnowledgeAnalyzer:
    def __init__(
        self,
        account_id: str,
        apikey: str,
        service_resource_id: str,
        knowledge_base_domain: str,
        feishu_webhook_url: str,
        keyword: str = "Talk"
    ):
        self.account_id = account_id
        self.apikey = apikey
        self.service_resource_id = service_resource_id
        self.knowledge_base_domain = knowledge_base_domain
        self.feishu_webhook_url = feishu_webhook_url
        self.keyword = keyword

    def _prepare_request(self, method: str, path: str, data: Optional[Dict] = None) -> requests.Request:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "Host": self.knowledge_base_domain,
            "Authorization": f"Bearer {self.apikey}"
        }
        
        req = requests.Request(
            method=method,
            url=f"https://{self.knowledge_base_domain}{path}",
            headers=headers,
            json=data if data else None
        )
        return req.prepare()

    def analyze_news(self, news_content: str, news_title: str = "") -> Optional[str]:
        """
        使用知识库大模型分析新闻
        """
        prompt = f"""
你已完整学习我上传的200个PDF资料库。
请基于资料库对以下新闻做深度分析与逻辑判断，严格遵守：
1. 提取新闻核心事实（3-5句）
2. 提炼资料库中相关逻辑、案例、规则（禁止摘抄原文）
3. 分析新闻与资料的一致性、矛盾点、原因、影响
4. 给出明确判断 + 依据
5. 预测趋势或给出建议

全程：深度思考、逻辑严密、不摘抄、基于资料。

新闻标题：{news_title}
新闻内容：
{news_content}
"""

        request_params = {
            "service_resource_id": self.service_resource_id,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "stream": False,
            "temperature": 0.7,
            "top_p": 0.9
        }

        try:
            session = requests.Session()
            req = self._prepare_request("POST", "/api/knowledge/service/chat", request_params)
            response = session.send(req, timeout=180)  # 增加超时时间到 3 分钟
            response.raise_for_status()
            result = response.json()
            
            if "data" in result and "generated_answer" in result["data"]:
                analysis_result = result["data"]["generated_answer"]
                logger.info(f"新闻分析成功: {news_title}")
                return analysis_result
            else:
                logger.error(f"API 响应结构错误: {result}")
                return None
        except Exception as e:
            logger.error(f"新闻分析失败: {e}", exc_info=True)
            return None

    def send_to_feishu(self, news_title: str, analysis_result: str, source: str = "") -> bool:
        """
        将分析结果发送到飞书
        """
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
    account_id: str,
    apikey: str,
    service_resource_id: str,
    knowledge_base_domain: str,
    feishu_webhook_url: str,
    keyword: str = "Talk"
):
    global _analyzer
    _analyzer = KnowledgeAnalyzer(
        account_id=account_id,
        apikey=apikey,
        service_resource_id=service_resource_id,
        knowledge_base_domain=knowledge_base_domain,
        feishu_webhook_url=feishu_webhook_url,
        keyword=keyword
    )
    logger.info(f"知识库分析器已初始化，关键词: '{keyword}'")


def get_knowledge_analyzer() -> Optional[KnowledgeAnalyzer]:
    return _analyzer
