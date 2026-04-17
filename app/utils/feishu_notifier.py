import time
import base64
import hmac
import hashlib
import httpx
import logging
import asyncio
from typing import List, Optional
from threading import Thread, Lock

logger = logging.getLogger(__name__)

LAST_SEND_TIME = 0.0
PUSH_COOLDOWN = 30
_pending_queue: List[str] = []
_draining = False
_lock = asyncio.Lock()


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
        global LAST_SEND_TIME, _pending_queue
        now = time.time()

        if now - LAST_SEND_TIME < PUSH_COOLDOWN:
            _pending_queue.append(content)
            wait_time = int(PUSH_COOLDOWN - (now - LAST_SEND_TIME))
            logger.warning(f"飞书推送冷却中，任务已缓存({len(_pending_queue)}条待发)，还需等待 {wait_time} 秒")
            self._start_drain_timer()
            return True

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
                    LAST_SEND_TIME = time.time()
                    logger.info("飞书推送成功")
                    return True
                else:
                    logger.warning(f"飞书推送失败: {result.get('msg')}")
                    return False
        except Exception as e:
            logger.error(f"飞书推送异常: {e}", exc_info=True)
            return False

    def _start_drain_timer(self):
        global _draining
        if _draining:
            return
        _draining = True

        def drain_later():
            time.sleep(PUSH_COOLDOWN)
            self._drain_queue()
            global _draining
            _draining = False

        t = Thread(target=drain_later, daemon=True)
        t.start()

    def _drain_queue(self):
        global LAST_SEND_TIME, _pending_queue
        if not _pending_queue:
            return

        now = time.time()
        if now - LAST_SEND_TIME < PUSH_COOLDOWN:
            wait_time = PUSH_COOLDOWN - (now - LAST_SEND_TIME)
            time.sleep(wait_time)

        while _pending_queue:
            content = _pending_queue.pop(0)
            LAST_SEND_TIME = time.time()
            logger.info(f"发送缓存任务: {content[:50]}...")
            self._do_send(content)

    def _do_send(self, content: str) -> bool:
        if self.keyword and self.keyword not in content:
            logger.info(f"消息中不包含关键词 '{self.keyword}'，跳过")
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
        else:
            params = {}

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(self.webhook_url, json=payload, params=params)
                result = response.json()
                if result.get("code") == 0:
                    logger.info("缓存任务推送成功")
                    return True
                else:
                    logger.warning(f"缓存任务推送失败: {result.get('msg')}")
                    return False
        except Exception as e:
            logger.error(f"缓存任务推送异常: {e}")
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

    def send_analysis(self, keyword: str, news_title: str, analysis_result: str, source: str = "") -> bool:
        """
        发送大模型分析结果到飞书
        """
        content_lines = [
            f"【{keyword}】📰 新闻深度分析",
            f"来源: {source}" if source else "",
            f"标题: {news_title}",
            "",
            "===== 分析结果 =====",
            analysis_result
        ]
        content = "\n".join([line for line in content_lines if line])
        return self.send_message(content)


def send_analysis_to_feishu(news_title: str, analysis_result: str, source: str = "", analyzer_type: str = "kb") -> bool:
    """
    统一发送分析结果到飞书
    analyzer_type: 'kb' = 豆包分析, 'openrouter' = OpenRouter分析
    """
    if analyzer_type == "openrouter":
        notifier = _openrouter_feishu_notifier
    else:
        notifier = _kb_feishu_notifier

    if not notifier:
        logger.warning(f"分析飞书未初始化({analyzer_type})，跳过推送")
        return False
    return notifier.send_analysis("Talk", news_title, analysis_result, source)


_feishu_notifier: Optional[FeishuNotifier] = None
_nyt_feishu_notifier: Optional[FeishuNotifier] = None
_bbc_feishu_notifier: Optional[FeishuNotifier] = None
_em_feishu_notifier: Optional[FeishuNotifier] = None
_index_feishu_notifier: Optional[FeishuNotifier] = None
_kb_feishu_notifier: Optional[FeishuNotifier] = None
_openrouter_feishu_notifier: Optional[FeishuNotifier] = None


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


def init_em_feishu_notifier(webhook_url: str, secret: str, keyword: str = "头条"):
    global _em_feishu_notifier
    _em_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"东方财富飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_index_feishu_notifier(webhook_url: str, secret: str, keyword: str = "指数"):
    global _index_feishu_notifier
    _index_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"指数飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_kb_feishu_notifier(webhook_url: str, secret: str, keyword: str = "Talk"):
    global _kb_feishu_notifier
    _kb_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"豆包分析飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_openrouter_feishu_notifier(webhook_url: str, secret: str, keyword: str = "Talk"):
    global _openrouter_feishu_notifier
    _openrouter_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"OpenRouter分析飞书推送已初始化，关键词: '{keyword}', webhook_url: {webhook_url}")


def init_all_notifiers(
    feishu_url: str = "",
    feishu_secret: str = "",
    feishu_keyword: str = "头条",
    nyt_url: str = "",
    nyt_keyword: str = "HOT",
    bbc_url: str = "",
    bbc_keyword: str = "HOT",
    em_url: str = "",
    em_keyword: str = "头条",
    index_url: str = "",
    index_keyword: str = "指数",
    kb_url: str = "",
    kb_keyword: str = "Talk",
    openrouter_url: str = "",
    openrouter_keyword: str = "Talk",
):
    """
    统一初始化所有飞书推送实例，新增飞书配置都在这里加
    """
    if feishu_url:
        init_feishu_notifier(feishu_url, feishu_secret, feishu_keyword)
    if nyt_url:
        init_nyt_feishu_notifier(nyt_url, "", nyt_keyword)
    if bbc_url:
        init_bbc_feishu_notifier(bbc_url, "", bbc_keyword)
    if em_url:
        init_em_feishu_notifier(em_url, "", em_keyword)
    if index_url:
        init_index_feishu_notifier(index_url, "", index_keyword)
    if kb_url:
        init_kb_feishu_notifier(kb_url, "", kb_keyword)
    if openrouter_url:
        init_openrouter_feishu_notifier(openrouter_url, "", openrouter_keyword)


def get_feishu_notifier() -> Optional[FeishuNotifier]:
    return _feishu_notifier


def get_nyt_feishu_notifier() -> Optional[FeishuNotifier]:
    return _nyt_feishu_notifier


def get_bbc_feishu_notifier() -> Optional[FeishuNotifier]:
    return _bbc_feishu_notifier


def get_em_feishu_notifier() -> Optional[FeishuNotifier]:
    return _em_feishu_notifier


def get_index_feishu_notifier() -> Optional[FeishuNotifier]:
    return _index_feishu_notifier


def get_kb_feishu_notifier() -> Optional[FeishuNotifier]:
    return _kb_feishu_notifier


def get_openrouter_feishu_notifier() -> Optional[FeishuNotifier]:
    return _openrouter_feishu_notifier


def send_with_cooldown(content: str) -> bool:
    """
    统一推送入口，带30秒冷却限制
    所有飞书推送都走这个函数
    """
    global _feishu_notifier, LAST_SEND_TIME
    notifier = _feishu_notifier
    if not notifier:
        logger.warning("飞书 notifier 未初始化，跳过推送")
        return False
    return notifier.send_message(content)


async def notify_index_alert(alert_content: str) -> bool:
    notifier = get_index_feishu_notifier()
    if notifier:
        return notifier.send_message(alert_content)
    return False


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


async def notify_em_news(news_list: List[dict], source: str) -> bool:
    notifier = get_em_feishu_notifier()
    if notifier:
        return notifier.send_news_notification(news_list, source)
    return False


async def notify_no_news() -> bool:
    notifier = get_feishu_notifier()
    if notifier:
        return notifier.send_no_news_notification()
    return False
