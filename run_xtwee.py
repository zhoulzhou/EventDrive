import json
import os
import tweepy
import httpx
from dotenv import load_dotenv

load_dotenv()

# ===================== 配置区 =====================
BEARER_TOKEN = os.getenv("X_B_T", "")
LIST_ID = os.getenv("X_LIST_ID", "")

MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
MAX_HISTORY_IDS = 50

FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
HISTORY_FILE = os.path.join(DATA_DIR, "tweet_history_ids.json")
# ==================================================

client = tweepy.Client(bearer_token=BEARER_TOKEN) if BEARER_TOKEN else None


def load_history_ids():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("ids", [])


def save_history_ids(id_list):
    trimmed = id_list[-MAX_HISTORY_IDS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": trimmed}, f, indent=2)


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


def fetch_list_tweets():
    if client is None or not LIST_ID:
        print("[配置错误] 请检查 X_B_T 和 X_LIST_ID 配置")
        return []

    history_ids = load_history_ids()
    print(f"[历史缓存] 当前 {len(history_ids)} / {MAX_HISTORY_IDS} 条")

    resp = client.get_list_tweets(
        id=LIST_ID,
        max_results=MAX_RESULTS,
        tweet_fields=["created_at", "text"]
    )
    tweet_list = resp.data if resp.data else []

    if not tweet_list:
        print("本次无推文返回")
        return []

    new_tweets = [t for t in tweet_list if str(t.id) not in history_ids]

    if not new_tweets:
        print("无新增推文（全部已在历史缓存中）")
        return []

    new_ids = [str(t.id) for t in new_tweets]
    save_history_ids(history_ids + new_ids)

    print(f"✅ 本次新增 {len(new_tweets)} 条 | 历史缓存: {min(len(history_ids) + len(new_ids), MAX_HISTORY_IDS)} / {MAX_HISTORY_IDS}")

    feishu_lines = [f"✅ 本次新增 {len(new_tweets)} 条推文", ""]
    for tweet in new_tweets:
        feishu_lines.append(f"推文ID：{tweet.id}")
        feishu_lines.append(f"发布时间：{tweet.created_at}")
        feishu_lines.append(f"正文：{tweet.text}")
        feishu_lines.append("-" * 40)
    send_to_feishu("\n".join(feishu_lines))

    for tweet in new_tweets:
        print(f"\n推文ID：{tweet.id}\n发布时间：{tweet.created_at}\n正文：{tweet.text}\n{'-'*60}")

    return new_tweets


if __name__ == "__main__":
    print("=== X列表推文抓取（50条历史去重版）===")
    print(f"目标列表ID：{LIST_ID} | 单次抓取：{MAX_RESULTS} 条 | 历史缓存：{MAX_HISTORY_IDS} 条")
    fetch_list_tweets()
