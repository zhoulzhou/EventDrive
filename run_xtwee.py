import json
import os
import time
from datetime import datetime
import tweepy
import httpx

# ===================== 配置区（填你可用的Bearer Token） =====================
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

LAST_TWEET_FILE = os.path.join(DATA_DIR, "last_global_tweet_id.json")
HISTORY_CACHE_FILE = os.path.join(DATA_DIR, "history_id_pool.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "daily_tweet_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "monthly_tweet_count.json")
# ===========================================================================


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


# 基础初始化，不额外发接口扣费
client = tweepy.Client(bearer_token=BEARER_TOKEN) if BEARER_TOKEN else None


# 永久全局增量游标（since_id，跨多次抓取共用）
def load_last_tweet_id():
    if not os.path.exists(LAST_TWEET_FILE):
        return 0
    with open(LAST_TWEET_FILE, "r", encoding="utf-8") as f:
        return int(json.load(f)["last_id"])


def save_last_tweet_id(new_max_id):
    with open(LAST_TWEET_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_max_id)}, f, indent=2)


# 历史ID缓存，上限100条，超出清空
def load_history_ids():
    if not os.path.exists(HISTORY_CACHE_FILE):
        return []
    with open(HISTORY_CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["ids"]


def save_history_ids(id_list):
    if len(id_list) >= MAX_HISTORY_CACHE_SIZE:
        print(f"[缓存已满] 历史ID达到{MAX_HISTORY_CACHE_SIZE}条，清空缓存池")
        write_data = {"ids": []}
    else:
        write_data = {"ids": id_list}
    with open(HISTORY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(write_data, f, indent=2)


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


def save_daily_stat(count, day_key):
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, indent=2)


# 月度抓取计数（成本封顶）
def get_month_stat():
    month_key = f"{datetime.now().year}-{datetime.now().month}"
    if not os.path.exists(MONTH_COUNT_FILE):
        json.dump({"month": month_key, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, month_key
    with open(MONTH_COUNT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data["month"] != month_key:
        json.dump({"month": month_key, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, month_key
    return int(data["count"]), month_key


def save_month_stat(count, month_key):
    with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": month_key, "count": count}, indent=2)


# 核心抓取逻辑（和你之前能跑通的Bearer请求逻辑完全一致）
def fetch_list_tweets():
    month_total, cur_month = get_month_stat()
    # 月度上限拦截，直接停止请求
    if month_total >= MONTH_MAX_LIMIT:
        msg = f"【停止抓取】本月已抓取{month_total}条，达到月度上限{MONTH_MAX_LIMIT}"
        print(msg)
        send_to_feishu(msg)
        return []

    day_total, cur_day = get_daily_stat()
    # 单日上限拦截
    if day_total >= DAY_MAX_LIMIT:
        msg = f"【跳过抓取】今日已抓取{day_total}条，达到当日上限{DAY_MAX_LIMIT}"
        print(msg)
        send_to_feishu(msg)
        return []

    last_id = load_last_tweet_id()
    req_params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": ["created_at", "text"]
        # 无 expansions，无用户信息附加计费
    }
    if last_id > 0:
        req_params["since_id"] = last_id
        print(f"【增量过滤】仅抓取ID > {last_id} 的新增推文，不重复拉取历史内容")
    else:
        print("【首次抓取】无历史游标，拉取列表最新内容")

    # 原生可用Bearer接口调用，无多余改动
    resp = client.get_list_tweets(id=LIST_ID, **req_params)
    tweet_arr = resp.data if resp.data else []

    # 无新推文，接口空返回，0扣费
    if not tweet_arr:
        msg = f"本次无新增推文 | 当日累计:{day_total}/{DAY_MAX_LIMIT} | 当月累计:{month_total}/{MONTH_MAX_LIMIT}"
        print(msg)
        return []

    new_tweet_count = len(tweet_arr)
    # 截断超出当日限额的数据
    if day_total + new_tweet_count > DAY_MAX_LIMIT:
        allow_num = DAY_MAX_LIMIT - day_total
        tweet_arr = tweet_arr[:allow_num]
        new_tweet_count = allow_num
        print(f"⚠️ 超出当日限额，仅保留最新{allow_num}条")

    # 更新永久全局游标ID
    latest_tweet_id = max(int(tw.id) for tw in tweet_arr)
    save_last_tweet_id(latest_tweet_id)

    # 更新100条容量限制的历史ID缓存
    old_history = load_history_ids()
    new_id_str_list = [str(tw.id) for tw in tweet_arr]
    merged_id_list = old_history + new_id_str_list
    save_history_ids(merged_id_list)

    # 更新日、月统计计数
    new_day_total = day_total + new_tweet_count
    new_month_total = month_total + new_tweet_count
    save_daily_stat(new_day_total, cur_day)
    save_month_stat(new_month_total, cur_month)

    result_msg = (f"✅ 本次新增{new_tweet_count}条列表推文 | "
                  f"当日累计{new_day_total}/{DAY_MAX_LIMIT} | "
                  f"当月累计{new_month_total}/{MONTH_MAX_LIMIT}\n"
                  f"📦 历史ID缓存总量：{len(merged_id_list)} / {MAX_HISTORY_CACHE_SIZE}")
    print(result_msg)

    # 推送飞书
    feishu_lines = [result_msg, ""]
    for tw in tweet_arr:
        feishu_lines.append(f"推文ID：{tw.id}")
        feishu_lines.append(f"发布时间：{tw.created_at}")
        feishu_lines.append(f"推文正文：{tw.text}")
        feishu_lines.append("-" * 60)
    send_to_feishu("\n".join(feishu_lines))

    for tw in tweet_arr:
        print(f"\n推文ID：{tw.id}")
        print(f"发布时间：{tw.created_at}")
        print(f"推文正文：{tw.text}\n" + "-"*60)
    return tweet_arr


# 定时主循环
if __name__ == "__main__":
    print("==============================================")
    print("Bearer公开列表抓取（原生可拉取版，新增100条缓存限制）")
    print(f"目标列表ID：{LIST_ID} | 单次条数：{MAX_RESULTS}")
    print(f"单日上限{DAY_MAX_LIMIT} | 月度上限{MONTH_MAX_LIMIT} | 历史ID缓存上限{MAX_HISTORY_CACHE_SIZE}")
    print("每日执行：0 / 6 / 12 / 18 整点0~10秒")
    if client is not None:
        fetch_list_tweets()
