import json
import os
import time
from datetime import datetime
import tweepy
import httpx

# ===================== 配置区 =====================
BEARER_TOKEN = os.getenv("X_B_T", "")
LIST_ID = os.getenv("X_LIST_ID", "")

# 抓取控制参数
MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))
MAX_HISTORY_CACHE_SIZE = 100

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 持久化文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

HISTORY_CACHE_FILE = os.path.join(DATA_DIR, "history_tweet_cache.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "list_day_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "list_month_count.json")
# ==================================================


def send_to_feishu(text):
    """推送文本到飞书"""
    if not FEISHU_WEBHOOK_URL:
        return False
    try:
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"【{FEISHU_KEYWORD}】{text}"
            }
        }
        with httpx.Client(timeout=15) as client:
            resp = client.post(FEISHU_WEBHOOK_URL, json=payload)
            return resp.json().get("code") == 0
    except Exception:
        return False


# OAuth2.0 App-only 客户端
client = tweepy.Client(bearer_token=BEARER_TOKEN) if BEARER_TOKEN else None


# 带容量限制的历史ID缓存（同时作为增量游标来源，取最大值为 since_id）
def load_history_cache():
    if not os.path.exists(HISTORY_CACHE_FILE):
        return []
    with open(HISTORY_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("ids", [])


def save_history_cache(id_list):
    if len(id_list) >= MAX_HISTORY_CACHE_SIZE:
        print(f"[缓存容量预警] 历史ID已达{MAX_HISTORY_CACHE_SIZE}条上限，清空历史缓存池")
        cache_data = {"ids": []}
    else:
        cache_data = {"ids": id_list}
    with open(HISTORY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2)


def get_last_tweet_id():
    """从历史缓存池中取最大ID作为增量游标"""
    ids = load_history_cache()
    if not ids:
        return 0
    return max(int(i) for i in ids)


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
        json.dump({"day": day_key, "count": count}, indent=2)


# 月度抓取计数（成本锁死）
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
        json.dump({"month": month_key, "count": count}, indent=2)


# 核心抓取函数
def fetch_list_tweets():
    # 月度上限拦截
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

    last_id = get_last_tweet_id()
    req_params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": ["created_at", "text"]
    }
    if last_id > 0:
        req_params["since_id"] = last_id
        print(f"[增量过滤] 仅抓取 ID > {last_id} 的新增推文")
    else:
        print("[首次运行/缓存已清空] 无历史游标，抓取最新推文")

    resp = client.get_list_tweets(id=LIST_ID, **req_params)
    tweet_list = resp.data if resp.data else []

    # 无新增推文，0扣费
    if not tweet_list:
        msg = f"本次无新增推文，无API扣费 | 当日累计:{day_total}/{DAY_MAX_LIMIT} | 当月累计:{month_total}/{MONTH_MAX_LIMIT}"
        print(msg)
        return []

    add_num = len(tweet_list)
    # 截断超出当日限额数据
    if day_total + add_num > DAY_MAX_LIMIT:
        allow_count = DAY_MAX_LIMIT - day_total
        tweet_list = tweet_list[:allow_count]
        add_num = allow_count
        print(f"⚠️ 超出当日限额，仅保留最新{allow_count}条")

    # 更新带容量限制的历史缓存（同时承担增量游标功能）
    history_ids = load_history_cache()
    new_ids = [str(t.id) for t in tweet_list]
    merged_ids = history_ids + new_ids
    save_history_cache(merged_ids)

    # 更新日/月抓取计数
    new_day_count = day_total + add_num
    new_month_count = month_total + add_num
    save_daily_count(new_day_count, cur_day)
    save_month_count(new_month_count, cur_month)

    current_cache_size = len(merged_ids) if len(merged_ids) < MAX_HISTORY_CACHE_SIZE else 0

    result_msg = (f"✅ 本次新增{add_num}条列表推文 | "
                  f"当日累计{new_day_count}/{DAY_MAX_LIMIT} | "
                  f"当月累计{new_month_count}/{MONTH_MAX_LIMIT}\n"
                  f"📦 历史ID缓存当前总量：{current_cache_size} / {MAX_HISTORY_CACHE_SIZE}")
    print(result_msg)

    # 推送飞书（带推文详情）
    feishu_lines = [result_msg, ""]
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
    print("=== 公开列表增量抓取启动（Bearer OAuth2.0，无用户UID依赖）===")
    print(f"目标列表ID：{LIST_ID} | 单次max_results={MAX_RESULTS} | 月度上限{MONTH_MAX_LIMIT}条")
    print(f"历史推文ID缓存最大容量：{MAX_HISTORY_CACHE_SIZE}条，满容量自动清空")
    fetch_list_tweets()
