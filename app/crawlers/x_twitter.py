import logging
from typing import List, Dict, Any

import tweepy
import httpx

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


MAX_HISTORY_IDS = 50
history_ids: List[str] = []

_client: tweepy.Client | None = None


def _get_client() -> tweepy.Client:
    global _client
    if _client is None:
        _client = tweepy.Client(bearer_token=settings.X_B_T)
    return _client


def _send_feishu(text: str) -> bool:
    if not settings.X_FEISHU_WEBHOOK_URL:
        return False
    try:
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"【{settings.X_FEISHU_KEYWORD}】{text}"
            }
        }
        with httpx.Client(timeout=15) as c:
            resp = c.post(settings.X_FEISHU_WEBHOOK_URL, json=payload)
            return resp.json().get("code") == 0
    except Exception as e:
        logger.warning(f"[X] 飞书推送失败: {e}")
        return False


def fetch_tweets() -> List[Dict[str, Any]]:
    global history_ids

    list_id = settings.X_LIST_ID
    if not list_id:
        logger.warning("[X] 未配置 X_LIST_ID，跳过抓取")
        return []

    if not settings.X_B_T:
        logger.warning("[X] 未配置 X_B_T，跳过抓取")
        return []

    logger.info(f"[X] 开始抓取列表推文, list_id={list_id}, 历史缓存={len(history_ids)}/{MAX_HISTORY_IDS}")

    try:
        client = _get_client()

        resp = client.get_list_tweets(
            id=list_id,
            max_results=settings.X_MAX_RESULTS,
            tweet_fields=["created_at", "text"],
        )
        tweet_objects = resp.data or []

        if not tweet_objects:
            logger.info("[X] 没有获取到推文")
            return []

        new_tweets = [t for t in tweet_objects if str(t.id) not in history_ids]

        if not new_tweets:
            logger.info("[X] 无新增推文（全部已在历史缓存中）")
            return []

        new_ids = [str(t.id) for t in new_tweets]
        history_ids = (history_ids + new_ids)[-MAX_HISTORY_IDS:]

        result: List[Dict[str, Any]] = []
        feishu_lines = [f"✅ 本次新增 {len(new_tweets)} 条推文", ""]

        for tw in new_tweets:
            tweet_id = int(tw.id)
            result.append({
                "id": tweet_id,
                "text": tw.text,
                "created_at": str(tw.created_at) if tw.created_at else None,
            })
            feishu_lines.append(f"推文ID：{tw.id}")
            feishu_lines.append(f"发布时间：{tw.created_at}")
            feishu_lines.append(f"正文：{tw.text}")
            feishu_lines.append("-" * 40)

        _send_feishu("\n".join(feishu_lines))

        logger.info(
            f"[X] 抓取完成，新增 {len(new_tweets)} 条推文，"
            f"历史缓存 {len(history_ids)}/{MAX_HISTORY_IDS}"
        )

        return result

    except Exception as e:
        logger.error(f"[X] 抓取推文失败: {e}", exc_info=True)
        return []
