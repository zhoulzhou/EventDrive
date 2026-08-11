import time
import base64
import hmac
import hashlib
import httpx
import logging
import asyncio
from typing import List, Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

PUSH_COOLDOWN = 2
_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()
_last_send_time: float = 0.0
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10)
    return _http_client


async def _queue_worker():
    global _last_send_time
    while True:
        try:
            item = await _queue.get()
            if item is None:
                _queue.task_done()
                break

            webhook_url, secret, keyword, content = item

            now = time.time()
            wait = _last_send_time + PUSH_COOLDOWN - now
            if wait > 0:
                await asyncio.sleep(wait)

            await _do_send(webhook_url, secret, keyword, content)
            _last_send_time = time.time()
            _queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"飞书队列消费异常: {e}", exc_info=True)
            try:
                _queue.task_done()
            except Exception:
                pass


def _ensure_worker():
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_queue_worker())


async def shutdown_notifier():
    global _queue, _worker_task, _http_client
    if _queue is not None:
        await _queue.put(None)
    if _worker_task is not None:
        try:
            await asyncio.wait_for(_worker_task, timeout=5)
        except Exception:
            _worker_task.cancel()
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()


class FeishuNotifier:
    def __init__(self, webhook_url: str, secret: str, keyword: str = "头条"):
        self.webhook_url = webhook_url
        self.secret = secret
        self.keyword = keyword

    def _generate_sign(self) -> Tuple[str, str]:
        timestamp = str(int(time.time()))
        string_to_sign = f'{timestamp}\n{self.secret}'
        hmac_code = hmac.new(
            self.secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        return timestamp, sign

    def _build_payload(self, content: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
        if self.keyword and self.keyword not in content:
            raise ValueError(f"消息中不包含关键词 '{self.keyword}'")

        payload = {
            "msg_type": "text",
            "content": {"text": content}
        }
        params: Dict[str, str] = {}
        if self.secret:
            timestamp, sign = self._generate_sign()
            params = {"timestamp": timestamp, "sign": sign}
        return payload, params

    async def send_message(self, content: str) -> bool:
        _ensure_worker()
        try:
            payload, params = self._build_payload(content)
        except ValueError as e:
            logger.info(str(e))
            return False

        async with _lock:
            global _last_send_time
            now = time.time()
            if _last_send_time > 0 and now - _last_send_time < PUSH_COOLDOWN:
                await _queue.put((self.webhook_url, self.secret, self.keyword, content))
                logger.info(f"飞书推送冷却中，任务已入队列(队列深度: {_queue.qsize()})")
                return True

        try:
            client = _get_http_client()
            response = await client.post(self.webhook_url, json=payload, params=params)
            result = response.json()
            if result.get("code") == 0:
                async with _lock:
                    _last_send_time = time.time()
                logger.debug("飞书推送成功")
                return True
            else:
                logger.warning(f"飞书推送失败: code={result.get('code')}, msg={result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"飞书推送异常: {e}")
            return False

    async def send_news_notification(self, news_list: List[dict], source: str, prefix: str = None) -> bool:
        if not news_list:
            logger.info(f"飞书通知: {source} 没有新闻，跳过")
            return False

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
        return await self.send_message(content)

    async def send_no_news_notification(self) -> bool:
        from datetime import datetime
        now = datetime.now().strftime("%H:%M")
        content = f"【{self.keyword}】📰 {now} 定时推送\n\n暂无新新闻"
        return await self.send_message(content)

    async def send_analysis(self, keyword: str, news_title: str, analysis_result: str, source: str = "") -> bool:
        content_lines = [
            f"【{keyword}】📰 新闻深度分析",
            f"来源: {source}" if source else "",
            f"标题: {news_title}",
            "",
            "===== 分析结果 =====",
            analysis_result
        ]
        content = "\n".join([line for line in content_lines if line])
        return await self.send_message(content)

    def send_message_sync(self, content: str) -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self.send_message(content), loop)
                return future.result(timeout=30)
            else:
                return loop.run_until_complete(self.send_message(content))
        except RuntimeError:
            return asyncio.run(self.send_message(content))

    def send_news_notification_sync(self, news_list: List[dict], source: str, prefix: str = None) -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self.send_news_notification(news_list, source, prefix), loop
                )
                return future.result(timeout=30)
            else:
                return loop.run_until_complete(self.send_news_notification(news_list, source, prefix))
        except RuntimeError:
            return asyncio.run(self.send_news_notification(news_list, source, prefix))


async def _do_send(webhook_url: str, secret: str, keyword: str, content: str) -> bool:
    if keyword and keyword not in content:
        logger.info(f"消息中不包含关键词 '{keyword}'，跳过")
        return False

    payload = {
        "msg_type": "text",
        "content": {"text": content}
    }
    params: Dict[str, str] = {}
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f'{timestamp}\n{secret}'
        hmac_code = hmac.new(
            secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        params = {"timestamp": timestamp, "sign": sign}

    try:
        client = _get_http_client()
        response = await client.post(webhook_url, json=payload, params=params)
        result = response.json()
        if result.get("code") == 0:
            logger.debug("飞书队列推送成功")
            return True
        else:
            logger.warning(f"飞书队列推送失败: code={result.get('code')}, msg={result.get('msg')}")
            return False
    except Exception as e:
        logger.error(f"飞书队列推送异常: {e}")
        return False


_nyt_feishu_notifier: Optional[FeishuNotifier] = None
_bbc_feishu_notifier: Optional[FeishuNotifier] = None
_dfcf_feishu_notifier: Optional[FeishuNotifier] = None
_index_feishu_notifier: Optional[FeishuNotifier] = None
_cls_feishu_notifier: Optional[FeishuNotifier] = None
_kb_feishu_notifier: Optional[FeishuNotifier] = None
_openrouter_feishu_notifier: Optional[FeishuNotifier] = None
_deepseek_feishu_notifier: Optional[FeishuNotifier] = None
_x_feishu_notifier: Optional[FeishuNotifier] = None


def init_nyt_feishu_notifier(webhook_url: str, secret: str, keyword: str = "HOT"):
    global _nyt_feishu_notifier
    _nyt_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"纽约时报飞书推送已初始化，关键词: '{keyword}'")


def init_bbc_feishu_notifier(webhook_url: str, secret: str, keyword: str = "HOT"):
    global _bbc_feishu_notifier
    _bbc_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"BBC飞书推送已初始化，关键词: '{keyword}'")


def init_dfcf_feishu_notifier(webhook_url: str, secret: str, keyword: str = "头条"):
    global _dfcf_feishu_notifier
    _dfcf_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"东方财富(dfcf)飞书推送已初始化，关键词: '{keyword}'")


def init_index_feishu_notifier(webhook_url: str, secret: str, keyword: str = "指数"):
    global _index_feishu_notifier
    _index_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"指数飞书推送已初始化，关键词: '{keyword}'")


def init_cls_feishu_notifier(webhook_url: str, secret: str, keyword: str = "头条"):
    global _cls_feishu_notifier
    _cls_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"财联社飞书推送已初始化，关键词: '{keyword}'")


def init_kb_feishu_notifier(webhook_url: str, secret: str, keyword: str = "Talk"):
    global _kb_feishu_notifier
    _kb_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"豆包分析飞书推送已初始化，关键词: '{keyword}'")


def init_openrouter_feishu_notifier(webhook_url: str, secret: str, keyword: str = "Talk"):
    global _openrouter_feishu_notifier
    _openrouter_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"OpenRouter分析飞书推送已初始化，关键词: '{keyword}'")


def init_deepseek_feishu_notifier(webhook_url: str, secret: str, keyword: str = "深度分析"):
    global _deepseek_feishu_notifier
    _deepseek_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"DeepSeek分析飞书推送已初始化，关键词: '{keyword}'")


def init_x_feishu_notifier(webhook_url: str, secret: str, keyword: str = "X推文"):
    global _x_feishu_notifier
    _x_feishu_notifier = FeishuNotifier(webhook_url, secret, keyword)
    logger.info(f"X推文飞书推送已初始化，关键词: '{keyword}'")


def init_all_notifiers(
    nyt_url: str = "",
    nyt_keyword: str = "HOT",
    bbc_url: str = "",
    bbc_keyword: str = "HOT",
    dfcf_url: str = "",
    dfcf_keyword: str = "头条",
    cls_url: str = "",
    cls_keyword: str = "头条",
    index_url: str = "",
    index_keyword: str = "指数",
    kb_url: str = "",
    kb_keyword: str = "Talk",
    openrouter_url: str = "",
    openrouter_keyword: str = "Talk",
    deepseek_url: str = "",
    deepseek_keyword: str = "深度分析",
    x_url: str = "",
    x_keyword: str = "X推文",
):
    if nyt_url:
        init_nyt_feishu_notifier(nyt_url, "", nyt_keyword)
    if bbc_url:
        init_bbc_feishu_notifier(bbc_url, "", bbc_keyword)
    if dfcf_url:
        init_dfcf_feishu_notifier(dfcf_url, "", dfcf_keyword)
    if cls_url:
        init_cls_feishu_notifier(cls_url, "", cls_keyword)
    if index_url:
        init_index_feishu_notifier(index_url, "", index_keyword)
    if kb_url:
        init_kb_feishu_notifier(kb_url, "", kb_keyword)
    if openrouter_url:
        init_openrouter_feishu_notifier(openrouter_url, "", openrouter_keyword)
    if deepseek_url:
        init_deepseek_feishu_notifier(deepseek_url, "", deepseek_keyword)
    if x_url:
        init_x_feishu_notifier(x_url, "", x_keyword)


def get_nyt_feishu_notifier() -> Optional[FeishuNotifier]:
    return _nyt_feishu_notifier


def get_bbc_feishu_notifier() -> Optional[FeishuNotifier]:
    return _bbc_feishu_notifier


def get_dfcf_feishu_notifier() -> Optional[FeishuNotifier]:
    return _dfcf_feishu_notifier


def get_index_feishu_notifier() -> Optional[FeishuNotifier]:
    return _index_feishu_notifier


def get_cls_feishu_notifier() -> Optional[FeishuNotifier]:
    return _cls_feishu_notifier


def get_kb_feishu_notifier() -> Optional[FeishuNotifier]:
    return _kb_feishu_notifier


def get_openrouter_feishu_notifier() -> Optional[FeishuNotifier]:
    return _openrouter_feishu_notifier


def get_deepseek_feishu_notifier() -> Optional[FeishuNotifier]:
    return _deepseek_feishu_notifier


def get_x_feishu_notifier() -> Optional[FeishuNotifier]:
    return _x_feishu_notifier


def x_feishu_notify(tweets: List[dict]) -> bool:
    notifier = get_x_feishu_notifier()
    if not notifier:
        logger.warning("X推文飞书 notifier 未初始化，跳过推送")
        return False

    if not tweets:
        logger.info("X推文飞书通知: 没有推文，跳过")
        return False

    header = f"【{notifier.keyword}】🐦 X 推文推送"
    content_lines = [
        header,
        f"共获取 {len(tweets)} 条推文",
        "",
    ]

    for idx, tweet in enumerate(tweets[:10], 1):
        tweet_id = tweet.get('id', '')
        tweet_time = tweet.get('created_at', '')
        text = tweet.get('text', '')

        content_lines.append(f"{idx}. ID: {tweet_id}")
        if tweet_time:
            content_lines.append(f"   时间: {tweet_time}")
        if text:
            content_lines.append(f"   内容: {text}")
        content_lines.append("")

    content = "\n".join(content_lines)
    return notifier.send_message_sync(content)


def x_feishu_status_notify(status_text: str) -> bool:
    notifier = get_x_feishu_notifier()
    if not notifier:
        logger.warning("X推文飞书 notifier 未初始化，跳过状态推送")
        return False
    content = f"【{notifier.keyword}】🐦 X 推文状态\n\n{status_text}"
    return notifier.send_message_sync(content)


def dfcf_feishu_notify(news_list: List[dict], source: str) -> bool:
    notifier = get_dfcf_feishu_notifier()
    if notifier:
        return notifier.send_news_notification_sync(news_list, source)
    return False


def cls_feishu_notify(news_list: List[dict], source: str) -> bool:
    notifier = get_cls_feishu_notifier()
    if notifier:
        return notifier.send_news_notification_sync(news_list, source)
    return False


def nyt_feishu_notify(news_list: List[dict], source: str) -> bool:
    notifier = get_nyt_feishu_notifier()
    if notifier:
        return notifier.send_news_notification_sync(news_list, source)
    return False


def bbc_feishu_notify(news_list: List[dict], source: str) -> bool:
    notifier = get_bbc_feishu_notifier()
    if notifier:
        return notifier.send_news_notification_sync(news_list, source)
    return False


def doubao_feishu_notify(news_title: str, analysis_result: str, source: str) -> bool:
    notifier = get_kb_feishu_notifier()
    if notifier:
        content_lines = [
            f"【Talk】📰 新闻深度分析",
            f"来源: {source}",
            f"标题: {news_title}",
            "",
            "===== 分析结果 =====",
            analysis_result
        ]
        content = "\n".join(content_lines)
        return notifier.send_message_sync(content)
    return False


def openrouter_feishu_notify(news_title: str, analysis_result: str, source: str, model: str = "") -> bool:
    notifier = get_openrouter_feishu_notifier()
    if notifier:
        content_lines = [
            f"【Talk】📰 新闻深度分析",
            f"来源: {source}",
            f"标题: {news_title}",
        ]
        if model:
            content_lines.append(f"模型: {model}")
        content_lines.extend([
            "",
            "===== 分析结果 =====",
            analysis_result
        ])
        content = "\n".join(content_lines)
        return notifier.send_message_sync(content)
    return False


def deepseek_feishu_notify(news_title: str, analysis_result: str, source: str) -> bool:
    notifier = get_deepseek_feishu_notifier()
    if notifier:
        content_lines = [
            f"【Talk】📰 DeepSeek V4 新闻分析",
            f"来源: {source}",
            f"标题: {news_title}",
            "",
            "===== 分析结果 =====",
            analysis_result
        ]
        content = "\n".join(content_lines)
        return notifier.send_message_sync(content)
    return False


def notify_index_alert_sync(alert_content: str) -> bool:
    notifier = get_index_feishu_notifier()
    if notifier:
        return notifier.send_message_sync(alert_content)
    return False


async def notify_index_alert(alert_content: str) -> bool:
    return notify_index_alert_sync(alert_content)
