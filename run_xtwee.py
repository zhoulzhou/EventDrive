import json
import os
import time
from datetime import datetime
import tweepy
import httpx

# ===================== 配置区（替换为你自己的Bearer Token） =====================
BEARER_TOKEN = os.getenv("X_B_T", "")
TARGET_LIST_ID = os.getenv("X_LIST_ID", "")

# 抓取成本控制参数
SINGLE_MAX = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))
MAX_HISTORY_ID_STORE = 100

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 持久化文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

GLOBAL_LAST_TWEET_ID = os.path.join(DATA_DIR, "last_global_tweet_id.json")
HISTORY_ID_POOL = os.path.join(DATA_DIR, "history_id_pool.json")
DAY_STAT_FILE = os.path.join(DATA_DIR, "list_daily_count.json")
MONTH_STAT_FILE = os.path.join(DATA_DIR, "list_month_count.json")
# ==============================================================================


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


# 1. 安全初始化客户端，分层捕获错误，避免client=None
client = None
# 第一步：创建客户端实例
try:
    client = tweepy.Client(bearer_token=BEARER_TOKEN)
except Exception as err:
    print(f"【初始化失败】Bearer Token格式错误：{err}")
    exit(1)

# 强制校验客户端非空
if client is None:
    print("【致命错误】Tweepy客户端实例为空，请核对Bearer Token")
    exit(1)

# 第二步：低成本校验Token有效性（仅查询列表基础信息，不计推文扣费）
try:
    list_meta = client.get_list(id=TARGET_LIST_ID)
    if not list_meta.data:
        print(f"【错误】列表ID {TARGET_LIST_ID} 不存在，请核对链接")
        exit(1)
    print(f"✅ Token校验通过，列表名称：{list_meta.data.name}")
except tweepy.Unauthorized:
    print("【权限错误】Bearer Token无访问该列表权限，请重新复制Token")
    exit(1)
except Exception as err:
    print(f"【网络/接口异常】{err}")
    exit(1)


# ---------------------- 持久化ID工具函数 ----------------------
# 永久全局最大推文ID（增量抓取核心，不受100条限制）
def load_global_last_id():
    if not os.path.exists(GLOBAL_LAST_TWEET_ID):
        return 0
    with open(GLOBAL_LAST_TWEET_ID, "r", encoding="utf-8") as f:
        return int(json.load(f)["last_id"])


def save_global_last_id(new_max_id):
    with open(GLOBAL_LAST_TWEET_ID, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_max_id)}, f, ensure_ascii=False, indent=2)


# 历史ID缓存池，最多存100条，满则清空
def load_history_ids():
    if not os.path.exists(HISTORY_ID_POOL):
        return []
    with open(HISTORY_ID_POOL, "r", encoding="utf-8") as f:
        return json.load(f)["ids"]


def save_history_ids(id_list):
    if len(id_list) >= MAX_HISTORY_ID_STORE:
        print(f"【缓存清理】历史ID已达上限{MAX_HISTORY_ID_STORE}条，清空缓存池")
        write_data = {"ids": []}
    else:
        write_data = {"ids": id_list}
    with open(HISTORY_ID_POOL, "w", encoding="utf-8") as f:
        json.dump(write_data, f, indent=2)


# ---------------------- 抓取计数统计工具 ----------------------
def get_daily_stat():
    today_key = f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"
    if not os.path.exists(DAY_STAT_FILE):
        json.dump({"day": today_key, "count": 0}, open(DAY_STAT_FILE, "w"), indent=2)
        return 0, today_key
    with open(DAY_STAT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data["day"] != today_key:
        json.dump({"day": today_key, "count": 0}, open(DAY_STAT_FILE, "w"), indent=2)
        return 0, today_key
    return int(data["count"]), today_key


def save_daily_stat(count, day_key):
    with open(DAY_STAT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, indent=2)


def get_month_stat():
    month_key = f"{datetime.now().year}-{datetime.now().month}"
    if not os.path.exists(MONTH_STAT_FILE):
        json.dump({"month": month_key, "count": 0}, open(MONTH_STAT_FILE, "w"), indent=2)
        return 0, month_key
    with open(MONTH_STAT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data["month"] != month_key:
        json.dump({"month": month_key, "count": 0}, open(MONTH_STAT_FILE, "w"), indent=2)
        return 0, month_key
    return int(data["count"]), month_key


def save_month_stat(count, month_key):
    with open(MONTH_STAT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": month_key, "count": count}, indent=2)


# ---------------------- 核心抓取逻辑 ----------------------
def fetch_target_list():
    month_total, cur_month = get_month_stat()
    # 月度上限拦截，直接停止请求
    if month_total >= MONTH_LIMIT:
        msg = f"【停止抓取】本月已抓取 {month_total} 条，达到月度上限 {MONTH_LIMIT}"
        print(msg)
        send_to_feishu(msg)
        return []

    day_total, cur_day = get_daily_stat()
    # 单日上限拦截
    if day_total >= DAY_LIMIT:
        msg = f"【跳过抓取】今日已抓取 {day_total} 条，达到当日上限 {DAY_LIMIT}"
        print(msg)
        send_to_feishu(msg)
        return []

    last_global_id = load_global_last_id()
    req_args = {
        "max_results": SINGLE_MAX,
        "tweet_fields": ["created_at", "text"]
        # 无 expansions，彻底移除每条0.01美元附加费
    }
    if last_global_id > 0:
        req_args["since_id"] = last_global_id
        print(f"【增量过滤】仅抓取 ID > {last_global_id} 的新增推文")
    else:
        print("【首次抓取】无历史缓存，拉取列表最新内容")

    # 标准接口调用，参数id传入列表ID，语法无错误
    resp = client.get_list_tweets(id=TARGET_LIST_ID, **req_args)
    tweet_arr = resp.data if resp.data else []

    # 无新推文，接口空返回，0扣费
    if not tweet_arr:
        msg = f"本次无新增推文 | 当日累计:{day_total}/{DAY_LIMIT} | 当月累计:{month_total}/{MONTH_LIMIT}"
        print(msg)
        return []

    new_tweet_count = len(tweet_arr)
    # 截断超出当日限额数据
    if day_total + new_tweet_count > DAY_LIMIT:
        allow_num = DAY_LIMIT - day_total
        tweet_arr = tweet_arr[:allow_num]
        new_tweet_count = allow_num
        print(f"⚠️ 超出当日限额，仅保留最新{allow_num}条")

    # 更新全局永久游标ID
    latest_tweet_id = max(int(tw.id) for tw in tweet_arr)
    save_global_last_id(latest_tweet_id)

    # 更新历史ID缓存池
    old_history = load_history_ids()
    new_id_str_list = [str(tw.id) for tw in tweet_arr]
    merged_id_list = old_history + new_id_str_list
    save_history_ids(merged_id_list)

    # 更新日/月统计计数
    new_day_total, _ = get_daily_stat()
    new_month_total, _ = get_month_stat()
    save_daily_stat(new_day_total + new_tweet_count, cur_day)
    save_month_stat(new_month_total + new_tweet_count, cur_month)

    final_day = new_day_total + new_tweet_count
    final_month = new_month_total + new_tweet_count
    cache_size = len(merged_id_list) if len(merged_id_list) < MAX_HISTORY_ID_STORE else 0

    result_msg = (f"✅ 本次新增{new_tweet_count}条列表推文 | "
                  f"当日累计{final_day}/{DAY_LIMIT} | "
                  f"当月累计{final_month}/{MONTH_LIMIT}\n"
                  f"📦 历史ID缓存池：{cache_size}/{MAX_HISTORY_ID_STORE}")
    print(result_msg)

    # 推送飞书
    feishu_lines = [result_msg, ""]
    for tw in tweet_arr:
        feishu_lines.append(f"推文ID：{tw.id}")
        feishu_lines.append(f"发布时间：{tw.created_at}")
        feishu_lines.append(f"正文：{tw.text}")
        feishu_lines.append("-" * 40)
    send_to_feishu("\n".join(feishu_lines))

    for tw in tweet_arr:
        print(f"\n推文ID：{tw.id}\n发布时间：{tw.created_at}\n正文：{tw.text}\n{'-'*60}")
    return tweet_arr


if __name__ == "__main__":
    print("=== 公开列表增量抓取启动（Bearer OAuth2.0，无用户UID依赖）===")
    print(f"目标列表ID：{TARGET_LIST_ID} | 单次max={SINGLE_MAX} | 月度上限{MONTH_LIMIT}条")
    print(f"历史ID缓存池最大容量：{MAX_HISTORY_ID_STORE}条，满容量自动清空")
    fetch_target_list()
