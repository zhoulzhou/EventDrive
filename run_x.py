"""
X 平台列表推文抓取脚本（tweepy + get_list_tweets）
- 直接调用 get_list_tweets 抓取指定列表推文
- 增量抓取（since_id 机制），避免重复扣费
- 月度/每日额度控制
- 飞书推送
- 详细日志输出
"""

import json
import os
import sys
import logging
import time
from datetime import datetime
from dotenv import load_dotenv
import tweepy
import httpx

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_x")

# ===================== 配置区 =====================
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# 硬编码你的列表ID
LIST_ID = os.getenv("X_LIST_ID", "")

# 抓取参数
MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 本地存储文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

LAST_TWEET_FILE = os.path.join(DATA_DIR, "last_list_tweet_id.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "list_day_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "list_month_count.json")
# ==================================================


def print_separator(title=""):
    line = "=" * 60
    if title:
        logger.info(f"\n{line}")
        logger.info(f"  {title}")
        logger.info(line)
    else:
        logger.info(f"\n{line}")


# 初始化OAuth1客户端（鉴权正常，已验证get_me可用）
client = tweepy.Client(
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET
)


# 读取上次最大推文ID（增量抓取）
def load_last_id():
    logger.debug(f"[状态] 读取 last_list_tweet_id: {LAST_TWEET_FILE}")
    if not os.path.exists(LAST_TWEET_FILE):
        logger.info("[状态] last_list_tweet_id.json 不存在，初始化为 0（首次运行）")
        return 0
    try:
        with open(LAST_TWEET_FILE, "r", encoding="utf-8") as f:
            last_id = int(json.load(f)["last_id"])
        logger.info(f"[状态] 上次抓取最大推文 ID: {last_id}")
        return last_id
    except Exception as e:
        logger.warning(f"[状态] 读取 last_id 失败: {e}，重置为 0")
        return 0


def save_last_id(new_id):
    logger.debug(f"[状态] 保存 last_list_tweet_id: {new_id}")
    with open(LAST_TWEET_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_id)}, f, indent=2)
    logger.info(f"[状态] last_list_tweet_id 已更新为: {new_id}")


# 日计数
def get_daily():
    today = f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"
    logger.debug(f"[当日计数] 今日日期: {today}")
    if not os.path.exists(DAY_COUNT_FILE):
        logger.info("[当日计数] list_day_count.json 不存在，初始化为 0")
        with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": today, "count": 0}, f, indent=2)
        return 0, today
    try:
        d = json.load(open(DAY_COUNT_FILE, "r", encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[当日计数] 读取失败: {e}，重置为 0")
        with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": today, "count": 0}, f, indent=2)
        return 0, today

    if d.get("day") != today:
        logger.info(f"[当日计数] 新的一天 ({d.get('day')} → {today})，自动清零")
        with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": today, "count": 0}, f, indent=2)
        return 0, today

    count = int(d.get("count", 0))
    logger.info(f"[当日计数] 今日已抓取: {count}/{DAY_MAX_LIMIT} 条")
    return count, today


# 月计数
def get_monthly():
    month = f"{datetime.now().year}-{datetime.now().month}"
    logger.debug(f"[月度计数] 当前月份: {month}")
    if not os.path.exists(MONTH_COUNT_FILE):
        logger.info("[月度计数] list_month_count.json 不存在，初始化为 0")
        with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": month, "count": 0}, f, indent=2)
        return 0, month
    try:
        d = json.load(open(MONTH_COUNT_FILE, "r", encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[月度计数] 读取失败: {e}，重置为 0")
        with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": month, "count": 0}, f, indent=2)
        return 0, month

    if d.get("month") != month:
        logger.info(f"[月度计数] 新月份到来 ({d.get('month')} → {month})，自动清零")
        with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": month, "count": 0}, f, indent=2)
        return 0, month

    count = int(d.get("count", 0))
    logger.info(f"[月度计数] 本月已抓取: {count}/{MONTH_MAX_LIMIT} 条")
    return count, month


def send_to_feishu(tweet_list):
    """将推文推送到飞书"""
    if not FEISHU_WEBHOOK_URL:
        logger.warning("[飞书] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    try:
        header = f"【{FEISHU_KEYWORD}】🐦 X平台列表推文"
        content_lines = [
            header,
            f"共获取 {len(tweet_list)} 条新推文",
            "",
        ]

        for idx, t in enumerate(tweet_list, 1):
            content_lines.append(f"【推文 {idx}】")
            content_lines.append(f"  ID: {t.id}")
            content_lines.append(f"  时间: {t.created_at}")
            content_lines.append(f"  内容: {t.text}")
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

        with httpx.Client(timeout=15) as http_client:
            response = http_client.post(FEISHU_WEBHOOK_URL, json=payload)
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


# 核心抓取函数，接口硬编码LIST_ID，无bug
def fetch_list_tweets():
    print_separator("X 平台列表推文抓取（tweepy + get_list_tweets）")

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        return []
    if not LIST_ID:
        logger.error("❌ [配置] 缺少 X_LIST_ID，请在 .env 中配置")
        return []

    logger.info(f"[配置] LIST_ID: {LIST_ID}")
    logger.info(f"[配置] MAX_RESULTS: {MAX_RESULTS}")
    logger.info(f"[配置] DAY_MAX_LIMIT: {DAY_MAX_LIMIT}")
    logger.info(f"[配置] MONTH_MAX_LIMIT: {MONTH_MAX_LIMIT}")

    # 月度额度检查
    print_separator("检查月度额度")
    month_cnt, cur_month = get_monthly()
    if month_cnt >= MONTH_MAX_LIMIT:
        logger.error(f"🛑 [暂停] 本月已抓取{month_cnt}条，到达月度上限")
        return []
    logger.info(f"✅ 月度额度充足: {month_cnt}/{MONTH_MAX_LIMIT}")

    # 当日额度检查
    print_separator("检查当日额度")
    day_cnt, cur_day = get_daily()
    if day_cnt >= DAY_MAX_LIMIT:
        logger.warning(f"⏭️ [跳过] 今日已抓取{day_cnt}条，到达当日上限")
        return []
    logger.info(f"✅ 当日额度充足: {day_cnt}/{DAY_MAX_LIMIT}")

    last_id = load_last_id()

    print_separator("调用 get_list_tweets 抓取列表推文")
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
    logger.info(f"📝 tweet_fields = ['created_at', 'text']")
    logger.info(f"📝 list_id = {LIST_ID}")

    # 硬编码列表ID调用列表推文接口，不会触发Bearer空值401
    try:
        resp = client.get_list_tweets(list_id=LIST_ID, **params)
    except Exception as e:
        logger.error(f"❌ API 请求异常: {e}", exc_info=True)
        return []

    tweets = resp.data if resp.data else []

    if not tweets:
        print_separator("抓取结果")
        logger.info(f"📭 无新推文 | 今日:{day_cnt}/{DAY_MAX_LIMIT} 本月:{month_cnt}/{MONTH_MAX_LIMIT} | ** 0 扣费 **")
        return []

    add = len(tweets)
    logger.info(f"📨 获取到 {add} 条推文")

    if day_cnt + add > DAY_MAX_LIMIT:
        allow = DAY_MAX_LIMIT - day_cnt
        logger.warning(f"⚠️ 当日剩余额度仅{allow}条，截断数据")
        tweets = tweets[:allow]
        add = allow

    max_tweet_id = max(int(t.id) for t in tweets)
    save_last_id(max_tweet_id)

    # 更新计数
    new_day, _ = get_daily()
    new_month, _ = get_monthly()
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": cur_day, "count": new_day + add}, f, indent=2)
    with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": cur_month, "count": new_month + add}, f, indent=2)

    logger.info(f"[当日计数] 更新为: {new_day + add}/{DAY_MAX_LIMIT}")
    logger.info(f"[月度计数] 更新为: {new_month + add}/{MONTH_MAX_LIMIT}")

    # 打印推文详情
    print_separator("推文详情")
    logger.info(f"📊 本次新增{add}条列表推文 | 当日累计{new_day + add}/{DAY_MAX_LIMIT} | 当月累计{new_month + add}/{MONTH_MAX_LIMIT}")
    for i, t in enumerate(tweets, 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  推文 #{i}")
        logger.info(f"  🆔 推文ID:   {t.id}")
        logger.info(f"  🕐 发布时间: {t.created_at}")
        logger.info(f"  📝 正文:     {t.text}")
        logger.info(f"{'─' * 50}")

    # 飞书推送
    print_separator("飞书推送")
    send_to_feishu(tweets)

    print_separator("抓取完成")
    return tweets


if __name__ == "__main__":
    start_time = datetime.now()
    logger.info(f"🚀 脚本启动时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        result = fetch_list_tweets()
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
