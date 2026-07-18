import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import tweepy

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
#  State file helpers
# ============================================================

def _state_path(filename: str) -> Path:
    """Return the full path for a state file inside DATA_DIR."""
    return settings.DATA_DIR / filename


# ---- last_list_tweet_id.json ----

def _load_last_tweet_id() -> int:
    """Load last fetched tweet ID. Auto-creates file with 0 if missing."""
    path = _state_path("last_list_tweet_id.json")
    if not path.exists():
        _save_last_tweet_id(0)
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get("last_id", 0))
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("[X] last_list_tweet_id.json 损坏，重置为 0")
        _save_last_tweet_id(0)
        return 0


def _save_last_tweet_id(tweet_id: int) -> None:
    """Persist last fetched tweet ID."""
    path = _state_path("last_list_tweet_id.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(tweet_id)}, f, ensure_ascii=False, indent=2)


# ---- x_month_count.json ----

def _get_month_key() -> str:
    """Return the current month key, e.g. '2026-7'."""
    now = datetime.now()
    return f"{now.year}-{now.month}"


def _load_month_count() -> int:
    """
    Load the monthly tweet count for the current month.
    Auto-creates file if missing; auto-resets on a new month.
    """
    path = _state_path("x_month_count.json")
    current_key = _get_month_key()

    if not path.exists():
        _save_month_count(0)
        return 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] x_month_count.json 损坏，重置为 0")
        _save_month_count(0)
        return 0

    if data.get("month") != current_key:
        _save_month_count(0)
        return 0

    return int(data.get("count", 0))


def _save_month_count(count: int) -> None:
    """Persist the monthly tweet count for the current month."""
    path = _state_path("x_month_count.json")
    current_key = _get_month_key()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"month": current_key, "count": count}, f, indent=2)


# ---- x_day_count.json ----

def _get_day_key() -> str:
    """Return the current day key, e.g. '2026-7-14'."""
    now = datetime.now()
    return f"{now.year}-{now.month}-{now.day}"


def _load_day_count() -> int:
    """
    Load the daily tweet count for the current day.
    Auto-creates file if missing; auto-resets on a new day.
    """
    path = _state_path("x_day_count.json")
    current_key = _get_day_key()

    if not path.exists():
        _save_day_count(0)
        return 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] x_day_count.json 损坏，重置为 0")
        _save_day_count(0)
        return 0

    if data.get("day") != current_key:
        _save_day_count(0)
        return 0

    return int(data.get("count", 0))


def _save_day_count(count: int) -> None:
    """Persist the daily tweet count for the current day."""
    path = _state_path("x_day_count.json")
    current_key = _get_day_key()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": current_key, "count": count}, f, indent=2)


# ============================================================
#  tweepy client
# ============================================================

_client: tweepy.Client | None = None


def _get_client() -> tweepy.Client:
    """Lazy-init tweepy Client with OAuth2.0 Bearer Token."""
    global _client
    if _client is None:
        _client = tweepy.Client(bearer_token=settings.X_BEARER_TOKEN)
    return _client


# ============================================================
#  Main fetch function
# ============================================================

def fetch_tweets() -> List[Dict[str, Any]]:
    """
    Fetch tweets from a specified list using tweepy get_list_tweets.
    Incremental fetching with monthly and daily rate limits.

    Returns:
        List of tweet dicts, each with keys: id, text, created_at.
        Returns an empty list when limits are exceeded or on error.
    """
    # ---- Check monthly limit ----
    month_count = _load_month_count()
    month_limit = settings.X_MONTH_MAX_LIMIT
    if month_count >= month_limit:
        logger.info(
            f"[X] 月度抓取已达上限 ({month_count}/{month_limit})，跳过"
        )
        return []

    # ---- Check daily limit ----
    day_count = _load_day_count()
    day_limit = settings.X_DAY_MAX_LIMIT
    if day_count >= day_limit:
        logger.info(
            f"[X] 每日抓取已达上限 ({day_count}/{day_limit})，跳过"
        )
        return []

    list_id = settings.X_LIST_ID
    if not list_id:
        logger.warning("[X] 未配置 X_LIST_ID，跳过抓取")
        return []

    if not settings.X_BEARER_TOKEN:
        logger.warning("[X] 未配置 X_BEARER_TOKEN，跳过抓取")
        return []

    last_tweet_id = _load_last_tweet_id()

    logger.info(
        f"[X] 开始抓取列表推文, list_id={list_id}, since_id={last_tweet_id}, "
        f"月度 {month_count}/{month_limit}, 每日 {day_count}/{day_limit}"
    )

    try:
        client = _get_client()

        params = {
            "max_results": settings.X_MAX_RESULTS,
            "tweet_fields": ["created_at", "text"],
        }
        if last_tweet_id > 0:
            params["since_id"] = last_tweet_id

        # 官方入参关键字是 id，不是 list_id
        resp = client.get_list_tweets(id=list_id, **params)
        tweet_objects = resp.data or []

        if not tweet_objects:
            logger.info("[X] 没有获取到新推文")
            return []

        # ---- Truncate to remaining daily capacity ----
        remaining = day_limit - day_count
        if len(tweet_objects) > remaining:
            logger.info(
                f"[X] 推文数量 ({len(tweet_objects)}) 超过每日剩余配额 ({remaining})，截断处理"
            )
            tweet_objects = tweet_objects[:remaining]

        # ---- Build result list ----
        result: List[Dict[str, Any]] = []
        max_id = last_tweet_id

        for tw in tweet_objects:
            tweet_id = int(tw.id)
            result.append({
                "id": tweet_id,
                "text": tw.text,
                "created_at": str(tw.created_at) if tw.created_at else None,
            })
            if tweet_id > max_id:
                max_id = tweet_id

        # ---- Persist state ----
        _save_last_tweet_id(max_id)

        new_month_count = month_count + len(result)
        _save_month_count(new_month_count)

        new_day_count = day_count + len(result)
        _save_day_count(new_day_count)

        logger.info(
            f"[X] 抓取完成，获取 {len(result)} 条推文，"
            f"月度累计 {new_month_count}/{month_limit}，每日累计 {new_day_count}/{day_limit}"
        )

        return result

    except Exception as e:
        logger.error(f"[X] 抓取推文失败: {e}", exc_info=True)
        return []
