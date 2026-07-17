"""
X 平台推文抓取测试脚本（纯手写 OAuth1 HMAC-SHA1 签名 + 数字UID版）
- 完全手动构造 OAuth1 Authorization Header，不依赖任何 oauth 库
- 100% 控制所有参数，不会自动注入多余字段
- 使用数字 UID 路径：/2/users/{USER_ID}/timelines/reverse_chronological
- 参数：tweet.fields
- 增量抓取（since_id 机制），避免重复扣费
- 月度/每日额度控制
- 飞书推送
- 详细日志输出
"""

import json
import os
import sys
import hmac
import base64
import hashlib
import logging
import time
from urllib.parse import urlencode, quote_plus
from datetime import datetime
from dotenv import load_dotenv
import requests

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_xtwetter")

# ===================== 密钥配置（四项必须弹窗完整复制Secret） =====================
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")
SELF_USER_ID = os.getenv("X_USER_ID", "")

# 抓取配置（接口强制max_results最小5）
MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 持久化文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

LAST_ID_FILE = os.path.join(DATA_DIR, "last_tweet_id.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "day_tweet_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "month_tweet_count.json")
# ===================================================================================


def print_separator(title=""):
    line = "=" * 60
    if title:
        logger.info(f"\n{line}")
        logger.info(f"  {title}")
        logger.info(line)
    else:
        logger.info(f"\n{line}")


# 读取上次最新推文ID
def load_last_tweet_id():
    logger.debug(f"[状态] 读取 last_tweet_id: {LAST_ID_FILE}")
    if not os.path.exists(LAST_ID_FILE):
        logger.info("[状态] last_tweet_id.json 不存在，初始化为 0（首次运行）")
        return 0
    try:
        with open(LAST_ID_FILE, "r", encoding="utf-8") as f:
            last_id = int(json.load(f).get("last_id", 0))
        logger.info(f"[状态] 上次抓取最大推文 ID: {last_id}")
        return last_id
    except Exception as e:
        logger.warning(f"[状态] 读取 last_tweet_id 失败: {e}，重置为 0")
        return 0


def save_last_tweet_id(new_id):
    logger.debug(f"[状态] 保存 last_tweet_id: {new_id}")
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_id)}, f, ensure_ascii=False, indent=2)
    logger.info(f"[状态] last_tweet_id 已更新为: {new_id}")


# 每日计数
def get_daily_stat():
    today_key = f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"
    logger.debug(f"[当日计数] 今日日期: {today_key}")
    if not os.path.exists(DAY_COUNT_FILE):
        logger.info("[当日计数] day_tweet_count.json 不存在，初始化为 0")
        return 0, today_key
    try:
        with open(DAY_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[当日计数] 读取失败: {e}，重置为 0")
        with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": today_key, "count": 0}, f, indent=2)
        return 0, today_key

    if data.get("day") != today_key:
        logger.info(f"[当日计数] 新的一天 ({data.get('day')} → {today_key})，自动清零")
        with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": today_key, "count": 0}, f, indent=2)
        return 0, today_key

    count = int(data.get("count", 0))
    logger.info(f"[当日计数] 今日已抓取: {count}/{DAY_MAX_LIMIT} 条")
    return count, today_key


def save_daily_stat(count, day_key):
    logger.debug(f"[当日计数] 保存: day={day_key}, count={count}")
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, f, indent=2)
    logger.info(f"[当日计数] 已更新: {count}/{DAY_MAX_LIMIT}")


# 月度计数
def get_month_stat():
    cur_month = f"{datetime.now().year}-{datetime.now().month}"
    logger.debug(f"[月度计数] 当前月份: {cur_month}")
    if not os.path.exists(MONTH_COUNT_FILE):
        logger.info("[月度计数] month_tweet_count.json 不存在，初始化为 0")
        return 0, cur_month
    try:
        with open(MONTH_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[月度计数] 读取失败: {e}，重置为 0")
        with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": cur_month, "count": 0}, f, indent=2)
        return 0, cur_month

    if data.get("month") != cur_month:
        logger.info(f"[月度计数] 新月份到来 ({data.get('month')} → {cur_month})，自动清零")
        with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"month": cur_month, "count": 0}, f, indent=2)
        return 0, cur_month

    count = int(data.get("count", 0))
    logger.info(f"[月度计数] 本月已抓取: {count}/{MONTH_MAX_LIMIT} 条")
    return count, cur_month


def save_month_stat(count, month_key):
    logger.debug(f"[月度计数] 保存: month={month_key}, count={count}")
    with open(MONTH_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"month": month_key, "count": count}, f, indent=2)
    logger.info(f"[月度计数] 已更新: {count}/{MONTH_MAX_LIMIT}")


def send_to_feishu(tweet_list):
    """将推文推送到飞书"""
    if not FEISHU_WEBHOOK_URL:
        logger.warning("[飞书] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    try:
        import httpx

        header = f"【{FEISHU_KEYWORD}】🐦 X平台推文"
        content_lines = [
            header,
            f"共获取 {len(tweet_list)} 条新推文",
            "",
        ]

        for idx, item in enumerate(tweet_list, 1):
            content_lines.append(f"【推文 {idx}】")
            content_lines.append(f"  ID: {item['id']}")
            content_lines.append(f"  时间: {item['created_at']}")
            content_lines.append(f"  内容: {item['text']}")
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


# 手动构造标准OAuth1 Authorization Header
def make_oauth_header(method, base_url, query_params):
    nonce = base64.b64encode(os.urandom(32)).decode().replace("+", "").replace("/", "").replace("=", "")
    ts = str(int(time.time()))

    logger.debug(f"[OAuth] nonce: {nonce[:20]}...")
    logger.debug(f"[OAuth] timestamp: {ts}")

    oauth_base = {
        "oauth_consumer_key": CONSUMER_KEY,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts,
        "oauth_token": ACCESS_TOKEN,
        "oauth_version": "1.0"
    }
    all_sign_params = {**oauth_base, **query_params}
    sorted_items = sorted(all_sign_params.items())
    param_str = "&".join([f"{quote_plus(k)}={quote_plus(str(v))}" for k, v in sorted_items])
    base_string = f"{method.upper()}&{quote_plus(base_url)}&{quote_plus(param_str)}"

    logger.debug(f"[OAuth] signature base string (前150字): {base_string[:150]}...")

    sign_key = f"{quote_plus(CONSUMER_SECRET)}&{quote_plus(ACCESS_TOKEN_SECRET)}"
    raw_sig = hmac.new(sign_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    sig = base64.b64encode(raw_sig).decode("utf-8")

    logger.debug(f"[OAuth] signature: {sig}")

    header_parts = [
        f'oauth_consumer_key="{quote_plus(CONSUMER_KEY)}"',
        f'oauth_nonce="{quote_plus(nonce)}"',
        f'oauth_signature="{quote_plus(sig)}"',
        'oauth_signature_method="HMAC-SHA1"',
        f'oauth_timestamp="{ts}"',
        f'oauth_token="{quote_plus(ACCESS_TOKEN)}"',
        'oauth_version="1.0"'
    ]
    auth_header = f"OAuth {', '.join(header_parts)}"
    logger.debug(f"[OAuth] Authorization header (前100字): {auth_header[:100]}...")
    return auth_header


# 核心抓取函数
def fetch_follow_timeline():
    print_separator("X 平台推文抓取测试（纯手写OAuth1 + 数字UID）")

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        return []
    if not SELF_USER_ID:
        logger.error("❌ [配置] 缺少 X_USER_ID（数字UID），请先运行 get_x_id.py 获取")
        return []

    logger.info(f"[配置] CONSUMER_KEY: {CONSUMER_KEY[:8]}... (已隐藏)")
    logger.info(f"[配置] SELF_USER_ID: {SELF_USER_ID}")
    logger.info(f"[配置] MAX_RESULTS: {MAX_RESULTS} (官方硬性下限5条)")
    logger.info(f"[配置] MONTH_MAX_LIMIT: {MONTH_MAX_LIMIT}")
    logger.info(f"[配置] DAY_MAX_LIMIT: {DAY_MAX_LIMIT}")

    # 1. 月度额度拦截
    print_separator("检查月度额度")
    month_cnt, cur_month = get_month_stat()
    if month_cnt >= MONTH_MAX_LIMIT:
        logger.error(f"🛑【停止】本月已抓取 {month_cnt} 条，达到月度上限 {MONTH_MAX_LIMIT}，本月不再请求API")
        return []
    logger.info(f"✅ 月度额度充足: {month_cnt}/{MONTH_MAX_LIMIT}")

    # 2. 当日额度拦截
    print_separator("检查当日额度")
    day_cnt, cur_day = get_daily_stat()
    if day_cnt >= DAY_MAX_LIMIT:
        logger.warning(f"⏭️【跳过】今日已抓取 {day_cnt} 条，达到当日限额 {DAY_MAX_LIMIT}，等待次日重置")
        return []
    logger.info(f"✅ 当日额度充足: {day_cnt}/{DAY_MAX_LIMIT}")

    # 合规路径：数字ID，无me
    base_api = f"https://api.x.com/2/users/{SELF_USER_ID}/timelines/reverse_chronological"
    last_id = load_last_tweet_id()

    # 仅官方允许的3个合法query参数，无任何非法字段
    query_params = {
        "max_results": MAX_RESULTS,
        "tweet.fields": "created_at,text",
    }
    if last_id > 0:
        query_params["since_id"] = last_id
        logger.info(f"📝 since_id = {last_id}（增量抓取）")
    else:
        logger.info("📝 since_id 为空（首次运行，抓取最新推文）")

    logger.info(f"📝 max_results = {MAX_RESULTS}")
    logger.info("📝 tweet.fields = 'created_at,text'")
    logger.info(f"📝 query_params 完整键: {list(query_params.keys())}")
    logger.info(f"📝 API 路径: /2/users/{SELF_USER_ID}/timelines/reverse_chronological")

    # 手动拼接查询串
    print_separator("构造 OAuth1 Authorization Header")
    query_str = urlencode(query_params)
    full_url = f"{base_api}?{query_str}"
    logger.info(f"🌐 完整请求 URL: {full_url}")

    auth_hdr = make_oauth_header("GET", base_api, query_params)
    headers = {"Authorization": auth_hdr}
    logger.info("🌐 Authorization header 构造完成")

    print_separator("发送 API 请求")
    logger.info("🌐 正在发送 GET 请求...")
    try:
        resp = requests.get(full_url, headers=headers, timeout=30)
        logger.info(f"🌐 响应状态码: {resp.status_code}")
    except Exception as e:
        logger.error(f"❌ API 请求异常: {e}", exc_info=True)
        return []

    try:
        resp_data = resp.json()
    except Exception as e:
        logger.error(f"❌ 响应 JSON 解析失败: {e}")
        logger.error(f"❌ 响应内容: {resp.text[:500]}")
        return []

    if resp.status_code == 401:
        logger.error(f"❌ [401鉴权失败] 密钥/Secret错误：{resp_data}")
        return []
    if resp.status_code == 402:
        logger.error(f"❌ [402 月度配额耗尽，停止抓取]")
        return []
    if resp.status_code != 200:
        logger.error(f"❌ [API异常] 状态码:{resp.status_code} 返回:{resp_data}")
        return []

    tweets = resp_data.get("data", [])

    if not tweets:
        print_separator("抓取结果")
        logger.info(f"📭 无新推文 | 日:{day_cnt}/{DAY_MAX_LIMIT} 月:{month_cnt}/{MONTH_MAX_LIMIT} | ** 0 扣费 **")
        return []

    add_num = len(tweets)
    logger.info(f"📨 获取到 {add_num} 条推文")

    if day_cnt + add_num > DAY_MAX_LIMIT:
        allow = DAY_MAX_LIMIT - day_cnt
        logger.warning(f"⚠️ 当日剩余额度仅{allow}条，截断数据")
        tweets = tweets[:allow]
        add_num = allow

    new_max_tweet_id = max(int(t["id"]) for t in tweets)
    save_last_tweet_id(new_max_tweet_id)

    new_day_total = day_cnt + add_num
    new_month_total = month_cnt + add_num
    save_daily_stat(new_day_total, cur_day)
    save_month_stat(new_month_total, cur_month)

    # 打印推文详情
    print_separator("推文详情")
    logger.info(f"📊 本次新增{add_num}条关注推文 | 当日累计{new_day_total}/{DAY_MAX_LIMIT} | 当月累计{new_month_total}/{MONTH_MAX_LIMIT}")
    for i, tw in enumerate(tweets, 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  推文 #{i}")
        logger.info(f"  🆔 ID:        {tw['id']}")
        logger.info(f"  🕐 发布时间:  {tw['created_at']}")
        logger.info(f"  📝 正文内容:  {tw['text']}")
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
        result = fetch_follow_timeline()
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
