"""
X 平台推文抓取测试脚本
- 使用 get_reverse_chronological_timeline 获取自己的关注时序流
- 增量抓取（since_id 机制），避免重复扣费
- 月度/每日额度控制
- 飞书推送
- 详细日志输出
"""

import tweepy
import json
import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_x")

# ==================== 配置区 ====================
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "3"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))

FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_ID_FILE = os.path.join(DATA_DIR, "last_tweet_id.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "month_tweet_count.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "day_tweet_count.json")
# =================================================


def print_separator(title=""):
    line = "=" * 60
    if title:
        logger.info(f"\n{line}")
        logger.info(f"  {title}")
        logger.info(line)
    else:
        logger.info(f"\n{line}")


def load_last_tweet_id():
    logger.debug(f"[状态] 读取 last_tweet_id: {STATE_ID_FILE}")
    if not os.path.exists(STATE_ID_FILE):
        logger.info("[状态] last_tweet_id.json 不存在，初始化为 0（首次运行）")
        return 0
    try:
        with open(STATE_ID_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        last_id = int(data.get("last_id", 0))
        logger.info(f"[状态] 上次抓取最大推文 ID: {last_id}")
        return last_id
    except Exception as e:
        logger.warning(f"[状态] 读取 last_tweet_id 失败: {e}，重置为 0")
        return 0


def save_last_tweet_id(new_id):
    logger.debug(f"[状态] 保存 last_tweet_id: {new_id}")
    with open(STATE_ID_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_id)}, f, ensure_ascii=False, indent=2)
    logger.info(f"[状态] last_tweet_id 已更新为: {new_id}")


def get_month_count():
    now = datetime.now()
    current_month = f"{now.year}-{now.month}"
    logger.debug(f"[月度计数] 当前月份: {current_month}")
    if not os.path.exists(MONTH_COUNT_FILE):
        logger.info("[月度计数] month_tweet_count.json 不存在，初始化为 0")
        return 0, current_month
    try:
        with open(MONTH_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[月度计数] 读取失败: {e}，重置为 0")
        save_month_count(0, current_month)
        return 0, current_month

    if data.get("month") != current_month:
        logger.info(f"[月度计数] 新月份到来 ({data.get('month')} → {current_month})，自动清零")
        save_month_count(0, current_month)
        return 0, current_month

    count = int(data.get("count", 0))
    logger.info(f"[月度计数] 本月已抓取: {count}/{MONTH_MAX_LIMIT} 条")
    return count, current_month


def save_month_count(count, month):
    logger.debug(f"[月度计数] 保存: month={month}, count={count}")
    with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": month, "count": count}, f, indent=2)
    logger.info(f"[月度计数] 已更新: {count}/{MONTH_MAX_LIMIT}")


def get_day_count():
    now = datetime.now()
    today_key = f"{now.year}-{now.month}-{now.day}"
    logger.debug(f"[当日计数] 今日日期: {today_key}")
    if not os.path.exists(DAY_COUNT_FILE):
        logger.info("[当日计数] day_tweet_count.json 不存在，初始化为 0")
        return 0, today_key
    try:
        with open(DAY_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[当日计数] 读取失败: {e}，重置为 0")
        save_day_count(0, today_key)
        return 0, today_key

    if data.get("day") != today_key:
        logger.info(f"[当日计数] 新的一天 ({data.get('day')} → {today_key})，自动清零")
        save_day_count(0, today_key)
        return 0, today_key

    count = int(data.get("count", 0))
    logger.info(f"[当日计数] 今日已抓取: {count}/{DAY_MAX_LIMIT} 条")
    return count, today_key


def save_day_count(count, day_key):
    logger.debug(f"[当日计数] 保存: day={day_key}, count={count}")
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, f, indent=2)
    logger.info(f"[当日计数] 已更新: {count}/{DAY_MAX_LIMIT}")


def send_to_feishu(tweets):
    """将推文推送到飞书"""
    if not FEISHU_WEBHOOK_URL:
        logger.warning("[飞书] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    try:
        import httpx
        import time
        import base64
        import hmac
        import hashlib

        header = f"【{FEISHU_KEYWORD}】🐦 X平台推文"
        content_lines = [
            header,
            f"共获取 {len(tweets)} 条新推文",
            "",
        ]

        for idx, tw in enumerate(tweets, 1):
            content_lines.append(f"【推文 {idx}】")
            content_lines.append(f"  ID: {tw.id}")
            content_lines.append(f"  时间: {tw.created_at}")
            content_lines.append(f"  内容: {tw.text}")
            content_lines.append("")

        content = "\n".join(content_lines)
        logger.info(f"[飞书] 准备推送，内容长度: {len(content)} 字符")

        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        logger.debug(f"[飞书] Webhook URL: {FEISHU_WEBHOOK_URL}")

        with httpx.Client(timeout=15) as client:
            response = client.post(FEISHU_WEBHOOK_URL, json=payload)
            result = response.json()
            logger.info(f"[飞书] 响应: code={result.get('code')}, msg={result.get('msg')}")

            if result.get("code") == 0:
                logger.info("✅ [飞书] 推送成功")
                return True
            else:
                logger.error(f"❌ [飞书] 推送失败: {result}")
                return False

    except Exception as e:
        logger.error(f"❌ [飞书] 推送异常: {e}", exc_info=True)
        return False


def fetch_timeline():
    print_separator("X 平台推文抓取测试")

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        return []

    logger.info(f"[配置] CONSUMER_KEY: {CONSUMER_KEY[:8]}... (已隐藏)")
    logger.info(f"[配置] MAX_RESULTS: {MAX_RESULTS}")
    logger.info(f"[配置] MONTH_MAX_LIMIT: {MONTH_MAX_LIMIT}")
    logger.info(f"[配置] DAY_MAX_LIMIT: {DAY_MAX_LIMIT}")

    # 1. 月度总额度拦截
    print_separator("检查月度额度")
    month_cnt, cur_month = get_month_count()
    if month_cnt >= MONTH_MAX_LIMIT:
        logger.error(f"🛑【停止】月度已抓取 {month_cnt} 条，达到总额度上限 {MONTH_MAX_LIMIT}，本月不再抓取")
        return []
    logger.info(f"✅ 月度额度充足: {month_cnt}/{MONTH_MAX_LIMIT}")

    # 2. 当日额度拦截
    print_separator("检查当日额度")
    day_cnt, cur_day = get_day_count()
    if day_cnt >= DAY_MAX_LIMIT:
        logger.warning(f"⏭️【跳过】今日已抓取 {day_cnt} 条，达到当日限额 {DAY_MAX_LIMIT}，等待明天")
        return []
    logger.info(f"✅ 当日额度充足: {day_cnt}/{DAY_MAX_LIMIT}")

    # 初始化API客户端
    print_separator("初始化 tweepy 客户端")
    try:
        client = tweepy.Client(
            consumer_key=CONSUMER_KEY,
            consumer_secret=CONSUMER_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET
        )
        logger.info("✅ tweepy 客户端初始化成功")
    except Exception as e:
        logger.error(f"❌ tweepy 客户端初始化失败: {e}", exc_info=True)
        return []

    # 准备请求参数
    print_separator("准备请求参数")
    last_id = load_last_tweet_id()
    params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": ["created_at", "text"]
    }
    if last_id > 0:
        params["since_id"] = last_id
        logger.info(f"📝 since_id = {last_id}（增量抓取）")
    else:
        logger.info("📝 since_id 为空（首次运行，抓取最新推文）")

    logger.info(f"📝 max_results = {MAX_RESULTS}")
    logger.info("📝 tweet_fields = ['created_at', 'text']（无 expansions，节省费用）")

    # 发起请求
    print_separator("调用 X API")
    logger.info("🌐 正在调用 get_reverse_chronological_timeline...")
    try:
        res = client.get_reverse_chronological_timeline(**params)
        logger.info(f"🌐 API 响应状态: data={'有' if res.data else '无'}, meta={res.meta}")
        new_tweets = res.data or []
    except Exception as e:
        logger.error(f"❌ API 请求失败: {e}", exc_info=True)
        return []

    if not new_tweets:
        print_separator("抓取结果")
        logger.info(f"📭 本次无新增推文，当日累计: {day_cnt} 条，当月累计: {month_cnt} 条，** 0 扣费 **")
        return []

    add_num = len(new_tweets)
    logger.info(f"📨 获取到 {add_num} 条推文")

    # 若本次抓取后超过当日限额，截断多余推文
    if day_cnt + add_num > DAY_MAX_LIMIT:
        allow_num = DAY_MAX_LIMIT - day_cnt
        logger.warning(f"⚠️ 当日剩余额度仅 {allow_num} 条，截断本次数据，只取最新 {allow_num} 条")
        new_tweets = new_tweets[:allow_num]
        add_num = allow_num

    # 更新最大推文ID
    new_max_id = max(tw.id for tw in new_tweets)
    save_last_tweet_id(new_max_id)

    # 更新日、月计数
    new_day_total = day_cnt + add_num
    new_month_total = month_cnt + add_num
    save_day_count(new_day_total, cur_day)
    save_month_count(new_month_total, cur_month)

    # 打印推文详情
    print_separator("推文详情")
    logger.info(f"📊 本次新增 {add_num} 条 | 当日累计 {new_day_total}/{DAY_MAX_LIMIT} | 当月累计 {new_month_total}/{MONTH_MAX_LIMIT}")
    for i, tw in enumerate(new_tweets, 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  推文 #{i}")
        logger.info(f"  🆔 ID:        {tw.id}")
        logger.info(f"  🕐 时间:      {tw.created_at}")
        logger.info(f"  📝 内容:      {tw.text}")
        logger.info(f"{'─' * 50}")

    # 飞书推送
    print_separator("飞书推送")
    send_to_feishu(new_tweets)

    print_separator("抓取完成")
    return new_tweets


if __name__ == "__main__":
    start_time = datetime.now()
    logger.info(f"🚀 脚本启动时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        result = fetch_timeline()
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        logger.info(f"🏁 脚本结束，总耗时: {duration:.2f} 秒，获取推文: {len(result)} 条")
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("⏹️ 用户中断")
        sys.exit(0)
    except Exception as e:
        logger.error(f"💥 脚本异常退出: {e}", exc_info=True)
        sys.exit(1)
