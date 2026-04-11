import time
import base64
import hmac
import hashlib
import httpx
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class FeishuNotifier:
    def __init__(self, webhook_url: str, secret: str, keyword: str = "头条"):
        self.webhook_url = webhook_url
        self.secret = secret
        self.keyword = keyword

    def _generate_sign(self) -> str:
        timestamp = str(int(time.time()))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        logger.info(f"签名计算: timestamp={timestamp}, string_to_sign='{string_to_sign}', secret_len={len(self.secret)}")
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        logger.info(f"签名结果: sign={sign}")
        return timestamp, sign

    def send_message(self, content: str) -> bool:
        logger.info(f"飞书推送检查: 关键词='{self.keyword}', 内容长度={len(content)}")

        if self.keyword and self.keyword not in content:
            logger.info(f"消息中不包含关键词 '{self.keyword}'，跳过推送")
            return False

        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        if self.secret:
            timestamp, sign = self._generate_sign()
            params = {"timestamp": timestamp, "sign": sign}
            logger.info(f"飞书推送请求(有签名): url={self.webhook_url}, timestamp={timestamp}, sign={sign}")
        else:
            params = {}
            logger.info(f"飞书推送请求(无签名): url={self.webhook_url}")

        logger.debug(f"飞书推送 payload: {payload}")

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(self.webhook_url, json=payload, params=params)
                result = response.json()
                logger.info(f"飞书推送响应: code={result.get('code')}, msg={result.get('msg')}, 状态码={response.status_code}")
                if result.get("code") == 0:
                    logger.info("飞书推送成功")
                    return True
                else:
                    logger.warning(f"飞书推送失败: {result.get('msg')}")
                    return False
        except Exception as e:
            logger.error(f"飞书推送异常: {e}", exc_info=True)
            return False

    def send_news_notification(self, news_list: List[dict], source: str, prefix: str = None) -> bool:
        if not news_list:
            logger.info(f"飞书通知: {source} 没有新闻，跳过")
            return False

        logger.info(f"飞书通知: {source} 准备发送 {len(news_list)} 条新闻")

        header = f"【{self.keyword}】📰 {source}" if not prefix else f"{prefix}📰 {source}"
        content_lines = [
            header,
            f"共获取 {len(news_list)} 条新闻",
            "",
        ]

        for idx, news in enumerate(news_list[:5], 1):
            title = news.get('title', '')
            summary = news.get('summary', '')
            publish_time = news.get('publish_time', '')

            content_lines.append(f"{idx}. {title}")
            if publish_time:
                content_lines.append(f"   {publish_time}")
            if summary:
                content_lines.append(f"   {summary}")
            content_lines.append("")

        content = "\n".join(content_lines)
        logger.info(f"飞书通知内容预览: {content[:500]}...")
        return self.send_message(content)

    def send_no_news_notification(self) -> bool:
        from datetime import datetime
        now = datetime.now().strftime("%H:%M")
        content = f"【{self.keyword}】📰 {now} 定时推送\n\n暂无新新闻"
        logger.info(f"飞书无新新闻通知: {content}")
        return self.send_message(content)


_feishu_notifier: Optional[FeishuNotifier] = None
_nyt_feishu_notifier: Optional[FeishuNotifier] = None
_bbc_feishu_notifier: Optional[FeishuNotifier] = None


def init_feishu_notifier(webhook_url: str, secret: str, keyword: str = "头条"):
    global _feishu_notifier
    _feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_nyt_feishu_notifier(webhook_url: str, secret: str, keyword: str = "HOT"):
    global _nyt_feishu_notifier
    _nyt_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"纽约时报飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_bbc_feishu_notifier(webhook_url: str, secret: str, keyword: str = "HOT"):
    global _bbc_feishu_notifier
    _bbc_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"BBC飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def get_feishu_notifier() -> Optional[FeishuNotifier]:
    return _feishu_notifier


def get_nyt_feishu_notifier() -> Optional[FeishuNotifier]:
    return _nyt_feishu_notifier


def get_bbc_feishu_notifier() -> Optional[FeishuNotifier]:
    return _bbc_feishu_notifier


async def notify_new_news(news_list: List[dict], source: str) -> bool:
    notifier = get_feishu_notifier()
    if notifier:
        return notifier.send_news_notification(news_list, source)
    return False


async def notify_nyt_news(news_list: List[dict], source: str) -> bool:
    notifier = get_nyt_feishu_notifier()
    if notifier:
        return notifier.send_news_notification(news_list, source)
    return False


async def notify_bbc_news(news_list: List[dict], source: str) -> bool:
    notifier = get_bbc_feishu_notifier()
    if notifier:
        return notifier.send_news_notification(news_list, source)
    return False


async def notify_no_news() -> bool:
    notifier = get_feishu_notifier()
    if notifier:
        return notifier.send_no_news_notification()
    return False
