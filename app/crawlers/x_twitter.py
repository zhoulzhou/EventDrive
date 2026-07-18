import logging
from typing import List, Dict, Any

import tweepy

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


def _get_since_id() -> str | None:
    if not history_ids:
        return None
    try:
        return str(max(int(x) for x in history_ids))
    except (ValueError, TypeError):
        return None


def fetch_tweets() -> List[Dict[str, Any]]:
    global history_ids

    list_id = settings.X_LIST_ID
    if not list_id:
        logger.warning("[X] 未配置 X_LIST_ID，跳过抓取")
        return []

    if not settings.X_B_T:
        logger.warning("[X] 未配置 X_B_T，跳过抓取")
        return []

    since_id = _get_since_id()
    logger.info(
        f"[X] 开始抓取列表推文, list_id={list_id}, "
        f"历史缓存={len(history_ids)}/{MAX_HISTORY_IDS}, "
        f"since_id={since_id or '无'}"
    )

    try:
        client = _get_client()

        resp = client.get_list_tweets(
            id=list_id,
            max_results=settings.X_MAX_RESULTS,
            tweet_fields=["created_at", "text"],
            since_id=since_id,
        )
        tweet_objects = resp.data or []

        if not tweet_objects:
            logger.info("[X] 没有获取到新推文")
            return []

        new_tweets = [t for t in tweet_objects if str(t.id) not in history_ids]

        if not new_tweets:
            logger.info("[X] 无新增推文")
            return []

        new_ids = [str(t.id) for t in new_tweets]
        combined = history_ids + new_ids

        if len(combined) > MAX_HISTORY_IDS:
            logger.info(
                f"[X] 历史缓存已满 ({len(combined)}/{MAX_HISTORY_IDS})，清空缓存并重置增量游标"
            )
            history_ids = new_ids[-MAX_HISTORY_IDS:]
        else:
            history_ids = combined

        result: List[Dict[str, Any]] = []

        for tw in new_tweets:
            tweet_id = int(tw.id)
            result.append({
                "id": tweet_id,
                "text": tw.text,
                "created_at": str(tw.created_at) if tw.created_at else None,
            })

        logger.info(
            f"[X] 抓取完成，新增 {len(new_tweets)} 条推文，"
            f"历史缓存 {len(history_ids)}/{MAX_HISTORY_IDS}"
        )

        return result

    except Exception as e:
        logger.error(f"[X] 抓取推文失败: {e}", exc_info=True)
        return []
