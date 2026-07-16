import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

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


# ---- last_tweet_id.json ----

def _load_last_tweet_id() -> int:
    """Load last fetched tweet ID. Auto-creates file with 0 if missing."""
    path = _state_path("last_tweet_id.json")
    if not path.exists():
        _save_last_tweet_id(0)
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("last_tweet_id", 0)
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] last_tweet_id.json 损坏，重置为 0")
        _save_last_tweet_id(0)
        return 0


def _save_last_tweet_id(tweet_id: int) -> None:
    """Persist last fetched tweet ID."""
    path = _state_path("last_tweet_id.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_tweet_id": tweet_id}, f, indent=2)


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

    if current_key not in data:
        # New month – reset
        _save_month_count(0)
        return 0

    return data.get(current_key, 0)


def _save_month_count(count: int) -> None:
    """Persist the monthly tweet count for the current month."""
    path = _state_path("x_month_count.json")
    current_key = _get_month_key()
    data = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError,):
            data = {}
    data[current_key] = count
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


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

    if current_key not in data:
        # New day – reset
        _save_day_count(0)
        return 0

    return data.get(current_key, 0)


def _save_day_count(count: int) -> None:
    """Persist the daily tweet count for the current day."""
    path = _state_path("x_day_count.json")
    current_key = _get_day_key()
    data = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError,):
            data = {}
    data[current_key] = count
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ============================================================
#  Main fetch function
# ============================================================

def fetch_tweets() -> List[Dict[str, Any]]:
    """
    Fetch tweets from home timeline using incremental fetching
    with monthly and daily rate limits.

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

    # ---- Build tweepy client ----
    client = tweepy.Client(
        consumer_key=settings.X_CONSUMER_KEY,
        consumer_secret=settings.X_CONSUMER_SECRET,
        access_token=settings.X_ACCESS_TOKEN,
        access_token_secret=settings.X_ACCESS_TOKEN_SECRET,
    )

    # ---- Determine since_id ----
    last_tweet_id = _load_last_tweet_id()
    since_id = last_tweet_id if last_tweet_id > 0 else None

    logger.info(
        f"[X] 开始抓取推文, since_id={since_id}, "
        f"月度 {month_count}/{month_limit}, 每日 {day_count}/{day_limit}"
    )

    try:
        # ---- Fetch tweets from X API ----
        kwargs: Dict[str, Any] = {
            "max_results": settings.X_MAX_RESULTS,
            "tweet_fields": ["created_at", "text"],
        }
        if since_id is not None:
            kwargs["since_id"] = since_id

        response = client.get_home_timeline(**kwargs)

        if response.data is None or len(response.data) == 0:
            logger.info("[X] 没有获取到新推文")
            return []

        tweets_data = response.data

        # ---- Truncate to remaining daily capacity ----
        remaining = day_limit - day_count
        if len(tweets_data) > remaining:
            logger.info(
                f"[X] 推文数量 ({len(tweets_data)}) 超过每日剩余配额 ({remaining})，截断处理"
            )
            tweets_data = tweets_data[:remaining]

        # ---- Build result list ----
        result: List[Dict[str, Any]] = []
        max_id = last_tweet_id

        for tweet in tweets_data:
            tweet_id = int(tweet.id)
            result.append({
                "id": tweet_id,
                "text": tweet.text,
                "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
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