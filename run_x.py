"""
X 平台推文抓取测试脚本
- 使用 requests_oauthlib + OAuth1Session 直接调用 X API v2
- 接口: /2/users/me/timelines/reverse_chronological
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
import requests
from requests_oauthlib import OAuth1Session

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_x")

# ===================== 密钥配置区（四项必须全部填完整，缺一不可） =====================
CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# 抓取额度配置
MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "3"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 本地持久化文件
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


# 读取上次抓取到的最新推文ID
def load_last_tweet_id():
    logger.debug(f"[状态] 读取 last_tweet_id: {LAST_ID_FILE}")
    if not os.path.exists(LAST_ID_FILE):
        logger.info("[状态] last_tweet_id.json 不存在，初始化为 0（首次运行）")
        return 0
    try:
        with open(LAST_ID_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        last_id = int(data.get("last_id", 0))
        logger.info(f"[状态] 上次抓取最大推文 ID: {last_id}")
        return last_id
    except Exception as e:
        logger.warning(f"[状态] 读取 last_tweet_id 失败: {e}，重置为 0")
        return 0


# 保存最新推文ID
def save_last_tweet_id(new_id):
    logger.debug(f"[状态] 保存 last_tweet_id: {new_id}")
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_id)}, f, ensure_ascii=False, indent=2)
    logger.info(f"[状态] last_tweet_id 已更新为: {new_id}")


# 读取当日抓取计数，每日0点自动重置
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
        save_daily_stat(0, today_key)
        return 0, today_key

    if data.get("day") != today_key:
        logger.info(f"[当日计数] 新的一天 ({data.get('day')} → {today_key})，自动清零")
        save_daily_stat(0, today_key)
        return 0, today_key

    count = int(data.get("count", 0))
    logger.info(f"[当日计数] 今日已抓取: {count}/{DAY_MAX_LIMIT} 条")
    return count, today_key


def save_daily_stat(count, day_key):
    logger.debug(f"[当日计数] 保存: day={day_key}, count={count}")
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, f, indent=2)
    logger.info(f"[当日计数] 已更新: {count}/{DAY_MAX_LIMIT}")


# 读取月度抓取计数，每月1号自动重置
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
        save_month_stat(0, cur_month)
        return 0, cur_month

    if data.get("month") != cur_month:
        logger.info(f"[月度计数] 新月份到来 ({data.get('month')} → {cur_month})，自动清零")
        save_month_stat(0, cur_month)
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


# 核心抓取函数
def fetch_follow_timeline():
    print_separator("X 平台推文抓取测试")

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, TOKEN_SECRET]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        return []

    logger.info(f"[配置] CONSUMER_KEY: {CONSUMER_KEY[:8]}... (已隐藏)")
    logger.info(f"[配置] MAX_RESULTS: {MAX_RESULTS}")
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

    # 初始化OAuth1鉴权会话
    print_separator("初始化 OAuth1 会话")
    try:
        oauth_session = OAuth1Session(
            CONSUMER_KEY,
            CONSUMER_SECRET,
            ACCESS_TOKEN,
            TOKEN_SECRET
        )
        logger.info("✅ OAuth1Session 初始化成功")
    except Exception as e:
        logger.error(f"❌ OAuth1Session 初始化失败: {e}", exc_info=True)
        return []

    api_url = "https://api.x.com/2/users/me/timelines/reverse_chronological"

    last_id = load_last_tweet_id()
    # 请求参数：仅拉取发布时间、正文，减少计费字段
    params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": "created_at,text"
    }
    # 增量抓取：只获取上次抓取之后新发布的推文
    if last_id > 0:
        params["since_id"] = last_id
        logger.info(f"📝 since_id = {last_id}（增量抓取）")
    else:
        logger.info("📝 since_id 为空（首次运行，抓取最新推文）")

    logger.info(f"📝 max_results = {MAX_RESULTS}")
    logger.info("📝 tweet_fields = 'created_at,text'（无 expansions，节省费用）")

    # 发送API请求
    print_separator("调用 X API v2")
    logger.info(f"🌐 请求 URL: {api_url}")
    logger.info("🌐 正在发送 GET 请求...")
    try:
        resp = oauth_session.get(api_url, params=params, timeout=30)
        logger.info(f"🌐 响应状态码: {resp.status_code}")
    except Exception as e:
        logger.error(f"❌ API 请求异常: {e}", exc_info=True)
        return []

    try:
        resp_json = resp.json()
    except Exception as e:
        logger.error(f"❌ 响应 JSON 解析失败: {e}")
        logger.error(f"❌ 响应内容: {resp.text[:500]}")
        return []

    # 捕获各类API错误
    if resp.status_code == 401:
        logger.error(f"❌ [401鉴权失败] 密钥缺失/Secret错误/Token失效，返回详情：{resp_json}")
        return []
    if resp.status_code == 402:
        logger.error(f"❌ [402扣费上限] 已达到月度1美元消费限额，本月停止抓取")
        return []
    if resp.status_code != 200:
        logger.error(f"❌ [API异常] 状态码:{resp.status_code} 完整返回:{resp_json}")
        return []

    tweet_list = resp_json.get("data", [])

    if not tweet_list:
        print_separator("抓取结果")
        logger.info(f"📭 本次无新增关注推文 | 当日累计:{day_cnt}/{DAY_MAX_LIMIT} | 当月累计:{month_cnt}/{MONTH_MAX_LIMIT} | ** 0 扣费 **")
        return []

    add_total = len(tweet_list)
    logger.info(f"📨 获取到 {add_total} 条推文")

    # 截断超出当日剩余额度的推文，避免单日超额扣费
    if day_cnt + add_total > DAY_MAX_LIMIT:
        allow_num = DAY_MAX_LIMIT - day_cnt
        logger.warning(f"⚠️ 当日剩余额度仅{allow_num}条，截断本次数据，仅保留最新{allow_num}条")
        tweet_list = tweet_list[:allow_num]
        add_total = allow_num

    # 更新全局最新推文ID
    new_max_id = max(int(item["id"]) for item in tweet_list)
    save_last_tweet_id(new_max_id)

    # 更新日、月抓取计数
    new_day_total = day_cnt + add_total
    new_month_total = month_cnt + add_total
    save_daily_stat(new_day_total, cur_day)
    save_month_stat(new_month_total, cur_month)

    # 打印推文详情
    print_separator("推文详情")
    logger.info(f"📊 本次新增{add_total}条关注推文 | 当日累计{new_day_total}/{DAY_MAX_LIMIT} | 当月累计{new_month_total}/{MONTH_MAX_LIMIT}")
    for i, item in enumerate(tweet_list, 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  推文 #{i}")
        logger.info(f"  🆔 ID:        {item['id']}")
        logger.info(f"  🕐 发布时间:  {item['created_at']}")
        logger.info(f"  📝 正文内容:  {item['text']}")
        logger.info(f"{'─' * 50}")

    # 飞书推送
    print_separator("飞书推送")
    send_to_feishu(tweet_list)

    print_separator("抓取完成")
    return tweet_list


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
