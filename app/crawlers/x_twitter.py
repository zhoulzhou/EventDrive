import json
import os
import hmac
import base64
import hashlib
import time
import logging
from urllib.parse import urlencode, quote_plus
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests

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
        return int(data.get("last_id", 0))
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("[X] last_tweet_id.json 损坏，重置为 0")
        _save_last_tweet_id(0)
        return 0


def _save_last_tweet_id(tweet_id: int) -> None:
    """Persist last fetched tweet ID."""
    path = _state_path("last_tweet_id.json")
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
#  OAuth1 HMAC-SHA1 Signature (pure Python, no library injection)
# ============================================================

def _make_oauth_header(method: str, base_url: str, query_params: dict) -> str:
    """
    Manually construct a standard OAuth1 Authorization header (RFC5849).
    Fully controls all parameters - no automatic injection of extra fields.
    """
    nonce = base64.b64encode(os.urandom(32)).decode().replace("+", "").replace("/", "").replace("=", "")
    ts = str(int(time.time()))

    oauth_base = {
        "oauth_consumer_key": settings.X_CONSUMER_KEY,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts,
        "oauth_token": settings.X_ACCESS_TOKEN,
        "oauth_version": "1.0",
    }
    all_sign_params = {**oauth_base, **query_params}
    sorted_items = sorted(all_sign_params.items())
    param_str = "&".join(
        f"{quote_plus(k)}={quote_plus(str(v))}" for k, v in sorted_items
    )
    base_string = f"{method.upper()}&{quote_plus(base_url)}&{quote_plus(param_str)}"

    sign_key = f"{quote_plus(settings.X_CONSUMER_SECRET)}&{quote_plus(settings.X_ACCESS_TOKEN_SECRET)}"
    raw_sig = hmac.new(
        sign_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    sig = base64.b64encode(raw_sig).decode("utf-8")

    header_parts = [
        f'oauth_consumer_key="{quote_plus(settings.X_CONSUMER_KEY)}"',
        f'oauth_nonce="{quote_plus(nonce)}"',
        f'oauth_signature="{quote_plus(sig)}"',
        'oauth_signature_method="HMAC-SHA1"',
        f'oauth_timestamp="{ts}"',
        f'oauth_token="{quote_plus(settings.X_ACCESS_TOKEN)}"',
        'oauth_version="1.0"',
    ]
    return f"OAuth {', '.join(header_parts)}"


# ============================================================
#  Main fetch function
# ============================================================

def fetch_tweets() -> List[Dict[str, Any]]:
    """
    Fetch tweets from reverse_chronological timeline using pure OAuth1 HMAC-SHA1 + X API v2.
    Uses numeric user ID path: /2/users/{USER_ID}/timelines/reverse_chronological
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

    last_tweet_id = _load_last_tweet_id()

    logger.info(
        f"[X] 开始抓取推文, since_id={last_tweet_id}, "
        f"月度 {month_count}/{month_limit}, 每日 {day_count}/{day_limit}"
    )

    try:
        # ---- Build API URL with numeric user ID ----
        user_id = settings.X_USER_ID
        base_api = f"https://api.x.com/2/users/{user_id}/timelines/reverse_chronological"

        query_params = {
            "max_results": settings.X_MAX_RESULTS,
            "tweet.fields": "created_at,text",
        }
        if last_tweet_id > 0:
            query_params["since_id"] = last_tweet_id

        # ---- Build full URL with query string ----
        query_str = urlencode(query_params)
        full_url = f"{base_api}?{query_str}"

        # ---- Build OAuth1 Authorization header ----
        auth_header = _make_oauth_header("GET", base_api, query_params)
        headers = {"Authorization": auth_header}

        # ---- Fetch tweets from X API ----
        resp = requests.get(full_url, headers=headers, timeout=30)

        try:
            resp_json = resp.json()
        except (ValueError, json.JSONDecodeError):
            logger.error(f"[X] API 返回非 JSON: status={resp.status_code}, text={resp.text[:300]}")
            return []

        if resp.status_code == 401:
            logger.error(f"[X] 401鉴权失败: 密钥缺失/Secret错误/Token失效, {resp_json}")
            return []
        if resp.status_code == 402:
            logger.error(f"[X] 402扣费上限: 已达到月度消费限额, {resp_json}")
            return []
        if resp.status_code != 200:
            logger.error(f"[X] API异常: status={resp.status_code}, {resp_json}")
            return []

        tweet_list = resp_json.get("data", [])

        if not tweet_list:
            logger.info("[X] 没有获取到新推文")
            return []

        # ---- Truncate to remaining daily capacity ----
        remaining = day_limit - day_count
        if len(tweet_list) > remaining:
            logger.info(
                f"[X] 推文数量 ({len(tweet_list)}) 超过每日剩余配额 ({remaining})，截断处理"
            )
            tweet_list = tweet_list[:remaining]

        # ---- Build result list ----
        result: List[Dict[str, Any]] = []
        max_id = last_tweet_id

        for item in tweet_list:
            tweet_id = int(item["id"])
            result.append({
                "id": tweet_id,
                "text": item.get("text", ""),
                "created_at": item.get("created_at"),
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
