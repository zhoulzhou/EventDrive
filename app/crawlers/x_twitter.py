import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import tweepy
import httpx

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


MAX_HISTORY_CACHE_SIZE = 100


def _state_path(filename: str) -> Path:
    return settings.DATA_DIR / filename


# ---- last_list_tweet.json (永久全局增量游标，不受100条缓存限制) ----

def _load_last_tweet_id() -> int:
    path = _state_path("last_list_tweet.json")
    if not path.exists():
        _save_last_tweet_id(0)
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get("last_id", 0))
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("[X] last_list_tweet.json 损坏，重置为 0")
        _save_last_tweet_id(0)
        return 0


def _save_last_tweet_id(tweet_id: int) -> None:
    path = _state_path("last_list_tweet.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(tweet_id)}, f, ensure_ascii=False, indent=2)


# ---- history_tweet_cache.json (带100条容量限制的历史ID缓存) ----

def _load_history_cache() -> List[str]:
    path = _state_path("history_tweet_cache.json")
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("ids", [])
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] history_tweet_cache.json 损坏，重置为空")
        return []


def _save_history_cache(id_list: List[str]) -> None:
    path = _state_path("history_tweet_cache.json")
    if len(id_list) >= MAX_HISTORY_CACHE_SIZE:
        logger.info(f"[X] 历史ID缓存已达{MAX_HISTORY_CACHE_SIZE}条上限，清空缓存池")
        cache_data = {"ids": []}
    else:
        cache_data = {"ids": id_list}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2)


# ---- list_month_count.json ----

def _get_month_key() -> str:
    now = datetime.now()
    return f"{now.year}-{now.month}"


def _load_month_count() -> int:
    path = _state_path("list_month_count.json")
    current_key = _get_month_key()
    if not path.exists():
        _save_month_count(0)
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] list_month_count.json 损坏，重置为 0")
        _save_month_count(0)
        return 0
    if data.get("month") != current_key:
        _save_month_count(0)
        return 0
    return int(data.get("count", 0))


def _save_month_count(count: int) -> None:
    path = _state_path("list_month_count.json")
    current_key = _get_month_key()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"month": current_key, "count": count}, f, indent=2)


# ---- list_day_count.json ----

def _get_day_key() -> str:
    now = datetime.now()
    return f"{now.year}-{now.month}-{now.day}"


def _load_day_count() -> int:
    path = _state_path("list_day_count.json")
    current_key = _get_day_key()
    if not path.exists():
        _save_day_count(0)
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        logger.warning("[X] list_day_count.json 损坏，重置为 0")
        _save_day_count(0)
        return 0
    if data.get("day") != current_key:
        _save_day_count(0)
        return 0
    return int(data.get("count", 0))


def _save_day_count(count: int) -> None:
    path = _state_path("list_day_count.json")
    current_key = _get_day_key()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": current_key, "count": count}, f, indent=2)


# ============================================================
#  Feishu notifier
# ============================================================

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
        with httpx.Client(timeout=15) as client:
            resp = client.post(settings.X_FEISHU_WEBHOOK_URL, json=payload)
            return resp.json().get("code") == 0
    except Exception as e:
        logger.warning(f"[X] 飞书推送失败: {e}")
        return False


# ============================================================
#  tweepy client
# ============================================================

_client: tweepy.Client | None = None


def _get_client() -> tweepy.Client:
    global _client
    if _client is None:
        _client = tweepy.Client(bearer_token=settings.X_B_T)
    return _client


# ============================================================
#  Main fetch function
# ============================================================

def fetch_tweets() -> List[Dict[str, Any]]:
    # 月度上限拦截
    month_count = _load_month_count()
    month_limit = settings.X_MONTH_MAX_LIMIT
    if month_count >= month_limit:
        msg = f"[月度成本锁定] 本月已抓取 {month_count} 条，达到上限 {month_limit}，停止本次请求"
        logger.info(msg)
        _send_feishu(msg)
        return []

    # 单日上限拦截
    day_count = _load_day_count()
    day_limit = settings.X_DAY_MAX_LIMIT
    if day_count >= day_limit:
        msg = f"[单日限额拦截] 今日已抓取 {day_count} 条，达到当日上限 {day_limit}"
        logger.info(msg)
        _send_feishu(msg)
        return []

    list_id = settings.X_LIST_ID
    if not list_id:
        logger.warning("[X] 未配置 X_LIST_ID，跳过抓取")
        return []

    if not settings.X_B_T:
        logger.warning("[X] 未配置 X_B_T，跳过抓取")
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

        resp = client.get_list_tweets(id=list_id, **params)
        tweet_objects = resp.data or []

        if not tweet_objects:
            logger.info("[X] 没有获取到新推文")
            return []

        # 截断超出当日限额数据
        remaining = day_limit - day_count
        add_num = len(tweet_objects)
        if add_num > remaining:
            tweet_objects = tweet_objects[:remaining]
            add_num = remaining
            logger.info(f"[X] 超出当日限额，仅保留最新{add_num}条")

        # 构建结果列表
        result: List[Dict[str, Any]] = []
        max_new_tid = last_tweet_id

        for tw in tweet_objects:
            tweet_id = int(tw.id)
            result.append({
                "id": tweet_id,
                "text": tw.text,
                "created_at": str(tw.created_at) if tw.created_at else None,
            })
            if tweet_id > max_new_tid:
                max_new_tid = tweet_id

        # 更新永久全局增量ID
        _save_last_tweet_id(max_new_tid)

        # 更新带100条容量限制的历史缓存
        history_ids = _load_history_cache()
        new_ids = [str(tw["id"]) for tw in result]
        merged_ids = history_ids + new_ids
        _save_history_cache(merged_ids)

        # 更新日/月抓取计数
        new_day_count = day_count + add_num
        new_month_count = month_count + add_num
        _save_day_count(new_day_count)
        _save_month_count(new_month_count)

        logger.info(
            f"[X] 抓取完成，获取 {add_num} 条推文，"
            f"月度累计 {new_month_count}/{month_limit}，每日累计 {new_day_count}/{day_limit}，"
            f"历史缓存 {len(merged_ids)}/{MAX_HISTORY_CACHE_SIZE}"
        )

        return result

    except Exception as e:
        logger.error(f"[X] 抓取推文失败: {e}", exc_info=True)
        return []
