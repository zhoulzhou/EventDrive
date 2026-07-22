import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

import tweepy

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


MAX_HISTORY_IDS = 50
history_ids: List[str] = []

day_count: int = 0
day_key: str = ""
month_count: int = 0
month_key: str = ""

_client: tweepy.Client | None = None


def _get_client() -> tweepy.Client:
    global _client
    if _client is None:
        _client = tweepy.Client(bearer_token=settings.X_B_T)
    return _client


def _check_day_reset() -> None:
    global day_count, day_key
    today = datetime.now().strftime("%Y-%m-%d")
    if day_key != today:
        day_key = today
        day_count = 0


def _check_month_reset() -> None:
    global month_count, month_key
    cur_month = datetime.now().strftime("%Y-%m")
    if month_key != cur_month:
        month_key = cur_month
        month_count = 0


def fetch_tweets() -> Dict[str, Any]:
    global history_ids, day_count, month_count

    result: Dict[str, Any] = {
        "status": "error",
        "tweets": [],
        "message": "",
        "day_count": 0,
        "day_limit": settings.X_DAY_MAX_LIMIT,
        "month_count": 0,
        "month_limit": settings.X_MONTH_MAX_LIMIT,
        "push_message": None,
    }

    list_id = settings.X_LIST_ID
    if not list_id:
        result["message"] = "未配置 X_LIST_ID，跳过抓取"
        result["push_message"] = f"⚠️ 配置错误\n{result['message']}"
        logger.warning(f"[X] {result['message']}")
        return result

    if not settings.X_B_T:
        result["message"] = "未配置 X_B_T，跳过抓取"
        result["push_message"] = f"⚠️ 配置错误\n{result['message']}"
        logger.warning(f"[X] {result['message']}")
        return result

    _check_day_reset()
    _check_month_reset()
    result["day_count"] = day_count
    result["month_count"] = month_count

    if month_count >= settings.X_MONTH_MAX_LIMIT:
        result["status"] = "monthly_limit"
        result["message"] = f"月度已抓取 {month_count} 条，达到总额度上限 {settings.X_MONTH_MAX_LIMIT}，本月不再抓取"
        result["push_message"] = (
            f"🛑 月度总额度已用完\n"
            f"当月: {month_count}/{settings.X_MONTH_MAX_LIMIT} 条\n"
            f"本月不再抓取"
        )
        logger.warning(f"[X] {result['message']}")
        return result

    if day_count >= settings.X_DAY_MAX_LIMIT:
        result["status"] = "daily_limit"
        result["message"] = f"今日已抓取 {day_count} 条，达到当日限额 {settings.X_DAY_MAX_LIMIT}，等待明天"
        result["push_message"] = (
            f"⏸️ 当日额度已用完\n"
            f"当日: {day_count}/{settings.X_DAY_MAX_LIMIT} 条\n"
            f"当月: {month_count}/{settings.X_MONTH_MAX_LIMIT} 条\n"
            f"等待明天继续"
        )
        logger.info(f"[X] {result['message']}")
        return result

    logger.info(
        f"[X] 开始抓取列表推文, list_id={list_id}, "
        f"历史缓存={len(history_ids)}/{MAX_HISTORY_IDS}, "
        f"当日={day_count}/{settings.X_DAY_MAX_LIMIT}, "
        f"当月={month_count}/{settings.X_MONTH_MAX_LIMIT}"
    )

    try:
        client = _get_client()

        resp = client.get_list_tweets(
            id=list_id,
            max_results=settings.X_MAX_RESULTS,
            tweet_fields=["created_at", "text"],
        )
        tweet_objects = resp.data or []

        if not tweet_objects:
            result["status"] = "no_new"
            result["message"] = "没有获取到新推文"
            result["push_message"] = (
                f"📭 无新增推文\n"
                f"当日: {day_count}/{settings.X_DAY_MAX_LIMIT} 条\n"
                f"当月: {month_count}/{settings.X_MONTH_MAX_LIMIT} 条"
            )
            logger.info(f"[X] {result['message']}")
            return result

        new_tweets = [t for t in tweet_objects if str(t.id) not in history_ids]

        if not new_tweets:
            result["status"] = "no_new"
            result["message"] = "无新增推文"
            result["push_message"] = (
                f"📭 无新增推文\n"
                f"当日: {day_count}/{settings.X_DAY_MAX_LIMIT} 条\n"
                f"当月: {month_count}/{settings.X_MONTH_MAX_LIMIT} 条"
            )
            logger.info(f"[X] {result['message']}")
            return result

        add_num = len(new_tweets)
        if day_count + add_num > settings.X_DAY_MAX_LIMIT:
            allow_num = settings.X_DAY_MAX_LIMIT - day_count
            logger.info(f"[X] 当日剩余额度仅 {allow_num} 条，截断本次数据，只取最新 {allow_num} 条")
            new_tweets = new_tweets[:allow_num]
            add_num = allow_num

        new_ids = [str(t.id) for t in new_tweets]
        combined = history_ids + new_ids

        if len(combined) > MAX_HISTORY_IDS:
            logger.info(
                f"[X] 历史缓存已满 ({len(combined)}/{MAX_HISTORY_IDS})，清空缓存只保留最新一批"
            )
            history_ids = new_ids[-MAX_HISTORY_IDS:]
        else:
            history_ids = combined

        day_count += add_num
        month_count += add_num

        tweet_list: List[Dict[str, Any]] = []
        push_lines = [f"🐦 X 推文推送", f"共获取 {len(new_tweets)} 条推文", ""]
        for idx, tw in enumerate(new_tweets, 1):
            tweet_id = int(tw.id)
            tweet_list.append({
                "id": tweet_id,
                "text": tw.text,
                "created_at": str(tw.created_at) if tw.created_at else None,
            })
            push_lines.append(f"{idx}. ID: {tw.id}")
            if tw.created_at:
                push_lines.append(f"   时间: {tw.created_at}")
            push_lines.append(f"   内容: {tw.text}")
            push_lines.append("")

        push_lines.append(f"当日: {day_count}/{settings.X_DAY_MAX_LIMIT} 条")
        push_lines.append(f"当月: {month_count}/{settings.X_MONTH_MAX_LIMIT} 条")

        result["status"] = "success"
        result["tweets"] = tweet_list
        result["day_count"] = day_count
        result["month_count"] = month_count
        result["message"] = f"抓取完成，新增 {len(new_tweets)} 条推文"
        result["push_message"] = "\n".join(push_lines)

        logger.info(
            f"[X] {result['message']}，"
            f"当日 {day_count}/{settings.X_DAY_MAX_LIMIT}，"
            f"当月 {month_count}/{settings.X_MONTH_MAX_LIMIT}，"
            f"历史缓存 {len(history_ids)}/{MAX_HISTORY_IDS}"
        )

        return result

    except Exception as e:
        result["status"] = "error"
        result["message"] = f"抓取推文失败: {e}"
        result["push_message"] = f"⚠️ 抓取异常\n{e}"
        logger.error(f"[X] {result['message']}", exc_info=True)
        return result
