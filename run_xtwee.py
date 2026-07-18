import json
import os
import time
from datetime import datetime
import tweepy
import httpx
from dotenv import load_dotenv

load_dotenv()

# ===================== 配置区 =====================
# OAuth2.0 Bearer Token（公开列表专用，无401权限坑）
BEARER_TOKEN = os.getenv("X_B_T", "")
# 目标公开列表ID
LIST_ID = os.getenv("X_LIST_ID", "")

# 抓取控制参数（成本锁死）
MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 持久化文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

LAST_TWEET_FILE = os.path.join(DATA_DIR, "last_list_tweet.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "list_day_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "list_month_count.json")
# ==================================================

# OAuth2.0 App-only 客户端，无expansions附加计费
client = tweepy.Client(bearer_token=BEARER_TOKEN) if BEARER_TOKEN else None


def send_to_feishu(text):
    if not FEISHU_WEBHOOK_URL:
        return False
    try:
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"【{FEISHU_KEYWORD}】{text}"
            }
        }
        with httpx.Client(timeout=15) as c:
            resp = c.post(FEISHU_WEBHOOK_URL, json=payload)
            return resp.json().get("code") == 0
    except Exception:
        return False


# 1、全局永久缓存：读取上次最大推文ID（since_id）
def load_last_tweet_id():
    if not os.path.exists(LAST_TWEET_FILE):
        return 0
    with open(LAST_TWEET_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        return int(data.get("last_id", 0))


# 更新全局最新推文ID
def save_last_tweet_id(new_tweet_id):
    with open(LAST_TWEET_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_tweet_id)}, f, ensure_ascii=False, indent=2)


# 当日抓取计数
def get_daily_stat():
    today_key = f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"
    if not os.path.exists(DAY_COUNT_FILE):
        json.dump({"day": today_key, "count": 0}, open(DAY_COUNT_FILE, "w"), indent=2)
        return 0, today_key
    with open(DAY_COUNT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data["day"] != today_key:
        json.dump({"day": today_key, "count": 0}, open(DAY_COUNT_FILE, "w"), indent=2)
        return 0, today_key
    return int(data["count"]), today_key


def save_daily_count(count, day_key):
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, f, indent=2)


# 当月抓取计数（月度成本锁死核心）
def get_month_stat():
    cur_month = f"{datetime.now().year}-{datetime.now().month}"
    if not os.path.exists(MONTH_COUNT_FILE):
        json.dump({"month": cur_month, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, cur_month
    with open(MONTH_COUNT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data["month"] != cur_month:
        json.dump({"month": cur_month, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, cur_month
    return int(data["count"]), cur_month


def save_month_count(count, month_key):
    with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": month_key, "count": count}, f, indent=2)


# 核心抓取函数
def fetch_list_tweets():
    # 4、月度上限拦截，到达直接停止请求
    month_total, cur_month = get_month_stat()
    if month_total >= MONTH_MAX_LIMIT:
        msg = f"[月度成本锁定] 本月已抓取 {month_total} 条，达到上限 {MONTH_MAX_LIMIT}，停止本次请求"
        print(msg)
        send_to_feishu(msg)
        return []

    # 单日上限拦截
    day_total, cur_day = get_daily_stat()
    if day_total >= DAY_MAX_LIMIT:
        msg = f"[单日限额拦截] 今日已抓取 {day_total} 条，达到当日上限 {DAY_MAX_LIMIT}"
        print(msg)
        send_to_feishu(msg)
        return []

    last_id = load_last_tweet_id()
    req_params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": ["created_at", "text"]
        # 无 expansions 参数，彻底砍掉0.01/条附加用户信息费
    }
    # 1、增量过滤：仅抓取上次缓存ID之后的新推文
    if last_id > 0:
        req_params["since_id"] = last_id
        print(f"[增量过滤] 仅抓取 ID > {last_id} 的新增推文")
    else:
        print("[首次运行] 无历史缓存，抓取最新5条推文")

    # 请求公开列表接口
    resp = client.get_list_tweets(id=LIST_ID, **req_params)
    tweet_list = resp.data if resp.data else []

    # 3、无新增推文，接口返回空，本次0扣费
    if not tweet_list:
        print(f"本次无新增推文，无API扣费 | 当日累计:{day_total}/{DAY_MAX_LIMIT} | 当月累计:{month_total}/{MONTH_MAX_LIMIT}")
        return []

    add_num = len(tweet_list)
    # 截断超出当日限额的数据
    if day_total + add_num > DAY_MAX_LIMIT:
        allow_count = DAY_MAX_LIMIT - day_total
        tweet_list = tweet_list[:allow_count]
        add_num = allow_count
        print(f"⚠️ 超出当日限额，仅保留最新{allow_count}条")

    # 更新全局最大推文ID（永久缓存，下次抓取复用）
    max_new_tid = max(int(tweet.id) for tweet in tweet_list)
    save_last_tweet_id(max_new_tid)

    # 更新日/月计数
    new_day_count = day_total + add_num
    new_month_count = month_total + add_num
    save_daily_count(new_day_count, cur_day)
    save_month_count(new_month_count, cur_month)

    print(f"✅ 本次新增{add_num}条列表推文 | 当日累计{new_day_count}/{DAY_MAX_LIMIT} | 当月累计{new_month_count}/{MONTH_MAX_LIMIT}")

    # 推送飞书
    feishu_lines = [f"✅ 本次新增{add_num}条列表推文 | 当日累计{new_day_count}/{DAY_MAX_LIMIT} | 当月累计{new_month_count}/{MONTH_MAX_LIMIT}", ""]
    for tweet in tweet_list:
        feishu_lines.append(f"推文ID：{tweet.id}")
        feishu_lines.append(f"发布时间：{tweet.created_at}")
        feishu_lines.append(f"正文：{tweet.text}")
        feishu_lines.append("-" * 40)
    send_to_feishu("\n".join(feishu_lines))

    for tweet in tweet_list:
        print(f"\n推文ID：{tweet.id}\n发布时间：{tweet.created_at}\n正文：{tweet.text}\n{'-'*60}")
    return tweet_list


# 定时主循环
if __name__ == "__main__":
    print("=== 公开列表增量抓取启动（Bearer OAuth2.0，成本管控版）===")
    print(f"目标列表ID：{LIST_ID} | 单次max_results={MAX_RESULTS} | 月度上限{MONTH_MAX_LIMIT}条")
    if client is not None:
        fetch_list_tweets()
