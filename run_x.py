"""
X 平台列表推文抓取测试脚本（tweepy 版）
- 使用 tweepy.Client + get_list_tweets 抓取指定列表推文
- 支持 show_my_all_lists() 查看所有自建列表ID
- 增量抓取（since_id 机制），避免重复扣费
- 月度/每日额度控制
- 飞书推送
- 详细日志输出
"""

import json
import os
import sys
import logging
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
CK = os.getenv("X_CONSUMER_KEY", "")
CS = os.getenv("X_CONSUMER_SECRET", "")
AT = os.getenv("X_ACCESS_TOKEN", "")
ATS = os.getenv("X_ACCESS_TOKEN_SECRET", "")

MAX_RESULTS = int(os.getenv("X_MAX_RESULTS", "5"))
DAY_MAX_LIMIT = int(os.getenv("X_DAY_MAX_LIMIT", "6"))
MONTH_MAX_LIMIT = int(os.getenv("X_MONTH_MAX_LIMIT", "190"))

# 列表ID（可通过 show_my_all_lists() 获取）
LIST_ID = os.getenv("X_LIST_ID", "")

# 飞书推送配置
FEISHU_WEBHOOK_URL = os.getenv("X_FEISHU_WEBHOOK_URL", "")
FEISHU_KEYWORD = os.getenv("X_FEISHU_KEYWORD", "X推文")

# 持久化文件
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

LAST_LIST_TWEET_FILE = os.path.join(DATA_DIR, "last_list_tweet.json")
DAY_COUNT_FILE = os.path.join(DATA_DIR, "day_count.json")
MONTH_COUNT_FILE = os.path.join(DATA_DIR, "month_count.json")
# ================================================


def print_separator(title=""):
    line = "=" * 60
    if title:
        logger.info(f"\n{line}")
        logger.info(f"  {title}")
        logger.info(line)
    else:
        logger.info(f"\n{line}")


client = tweepy.Client(
    consumer_key=CK,
    consumer_secret=CS,
    access_token=AT,
    access_token_secret=ATS
)


# 读取列表上次抓取最大推文ID
def load_last_list_id():
    logger.debug(f"[状态] 读取 last_list_tweet_id: {LAST_LIST_TWEET_FILE}")
    if not os.path.exists(LAST_LIST_TWEET_FILE):
        logger.info("[状态] last_list_tweet.json 不存在，初始化为 0（首次运行）")
        return 0
    try:
        with open(LAST_LIST_TWEET_FILE, "r", encoding="utf-8") as f:
            last_id = int(json.load(f).get("last_id", 0))
        logger.info(f"[状态] 上次抓取最大推文 ID: {last_id}")
        return last_id
    except Exception as e:
        logger.warning(f"[状态] 读取 last_list_tweet_id 失败: {e}，重置为 0")
        return 0


def save_last_list_id(new_id):
    logger.debug(f"[状态] 保存 last_list_tweet_id: {new_id}")
    with open(LAST_LIST_TWEET_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": str(new_id)}, f, ensure_ascii=False, indent=2)
    logger.info(f"[状态] last_list_tweet_id 已更新为: {new_id}")


# 日/月计数逻辑
def get_daily_stat():
    today = f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"
    logger.debug(f"[当日计数] 今日日期: {today}")
    if not os.path.exists(DAY_COUNT_FILE):
        logger.info("[当日计数] day_count.json 不存在，初始化为 0")
        return 0, today
    try:
        d = json.load(open(DAY_COUNT_FILE, "r", encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[当日计数] 读取失败: {e}，重置为 0")
        json.dump({"day": today, "count": 0}, open(DAY_COUNT_FILE, "w"), indent=2)
        return 0, today
    if d["day"] != today:
        logger.info(f"[当日计数] 新的一天 ({d.get('day')} → {today})，自动清零")
        json.dump({"day": today, "count": 0}, open(DAY_COUNT_FILE, "w"), indent=2)
        return 0, today
    count = int(d.get("count", 0))
    logger.info(f"[当日计数] 今日已抓取: {count}/{DAY_MAX_LIMIT} 条")
    return count, today


def save_daily_stat(count, day_key):
    logger.debug(f"[当日计数] 保存: day={day_key}, count={count}")
    with open(DAY_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"day": day_key, "count": count}, f, indent=2)
    logger.info(f"[当日计数] 已更新: {count}/{DAY_MAX_LIMIT}")


def get_month_stat():
    cur_month = f"{datetime.now().year}-{datetime.now().month}"
    logger.debug(f"[月度计数] 当前月份: {cur_month}")
    if not os.path.exists(MONTH_COUNT_FILE):
        logger.info("[月度计数] month_count.json 不存在，初始化为 0")
        return 0, cur_month
    try:
        d = json.load(open(MONTH_COUNT_FILE, "r", encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[月度计数] 读取失败: {e}，重置为 0")
        json.dump({"month": cur_month, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, cur_month
    if d["month"] != cur_month:
        logger.info(f"[月度计数] 新月份到来 ({d.get('month')} → {cur_month})，自动清零")
        json.dump({"month": cur_month, "count": 0}, open(MONTH_COUNT_FILE, "w"), indent=2)
        return 0, cur_month
    count = int(d.get("count", 0))
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
        header = f"【{FEISHU_KEYWORD}】🐦 X平台列表推文"
        content_lines = [
            header,
            f"共获取 {len(tweet_list)} 条新推文",
            "",
        ]

        for idx, tw in enumerate(tweet_list, 1):
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


# 抓取指定列表推文
def fetch_list_timeline():
    print_separator("X 平台列表推文抓取（tweepy 版）")

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CK, CS, AT, ATS]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        return []
    if not LIST_ID:
        logger.error("❌ [配置] 缺少 X_LIST_ID（列表ID），请先运行 show_my_all_lists() 获取")
        return []

    logger.info(f"[配置] CONSUMER_KEY: {CK[:8]}... (已隐藏)")
    logger.info(f"[配置] LIST_ID: {LIST_ID}")
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

    last_id = load_last_list_id()

    print_separator("调用 get_list_tweets 抓取列表推文")
    logger.info(f"📝 list_id = {LIST_ID}")
    logger.info(f"📝 max_results = {MAX_RESULTS}")
    logger.info(f"📝 tweet.fields = ['created_at', 'text']")
    if last_id > 0:
        logger.info(f"📝 since_id = {last_id}（增量抓取）")
    else:
        logger.info("📝 since_id 为空（首次运行，抓取最新推文）")

    params = {
        "max_results": MAX_RESULTS,
        "tweet_fields": ["created_at", "text"]
    }
    if last_id > 0:
        params["since_id"] = last_id

    # 核心：获取列表推文
    try:
        resp = client.get_list_tweets(list_id=LIST_ID, **params)
    except Exception as e:
        logger.error(f"❌ API 请求异常: {e}", exc_info=True)
        return []

    tweets = resp.data if resp.data else []

    if not tweets:
        print_separator("抓取结果")
        logger.info(f"📭 列表无新推文 | 日:{day_cnt}/{DAY_MAX_LIMIT} 月:{month_cnt}/{MONTH_MAX_LIMIT} | ** 0 扣费 **")
        return []

    add = len(tweets)
    logger.info(f"📨 获取到 {add} 条推文")

    if day_cnt + add > DAY_MAX_LIMIT:
        allow = DAY_MAX_LIMIT - day_cnt
        logger.warning(f"⚠️ 当日剩余额度仅{allow}条，截断数据")
        tweets = tweets[:allow]
        add = allow

    new_max = max(int(t.id) for t in tweets)
    save_last_list_id(new_max)

    # 更新计数
    new_day_total = day_cnt + add
    new_month_total = month_cnt + add
    save_daily_stat(new_day_total, cur_day)
    save_month_stat(new_month_total, cur_month)

    # 打印推文详情
    print_separator("推文详情")
    logger.info(f"📊 列表本次新增{add}条推文 | 当日累计{new_day_total}/{DAY_MAX_LIMIT} | 当月累计{new_month_total}/{MONTH_MAX_LIMIT}")
    for i, tw in enumerate(tweets, 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  推文 #{i}")
        logger.info(f"  🆔 ID:        {tw.id}")
        logger.info(f"  🕐 发布时间:  {tw.created_at}")
        logger.info(f"  📝 正文内容:  {tw.text}")
        logger.info(f"{'─' * 50}")

    # 飞书推送
    print_separator("飞书推送")
    send_to_feishu(tweets)

    print_separator("抓取完成")
    return tweets


# 先执行一次，获取你所有自建列表ID（运行一次即可）
def show_my_all_lists():
    print_separator("获取所有自建列表")

    logger.info("[配置] 检查 API 凭证...")
    if not all([CK, CS, AT, ATS]):
        logger.error("❌ [配置] 缺少 API 凭证")
        return

    try:
        me = client.get_me()
        my_uid = me.data.id
        logger.info(f"✅ 用户数字 UID: {my_uid}")
        logger.info(f"✅ 用户名: @{me.data.username}")
    except Exception as e:
        logger.error(f"❌ 获取用户信息失败: {e}")
        return

    # 获取自己创建的全部列表
    try:
        lists_resp = client.get_owned_lists(id=my_uid)
    except Exception as e:
        logger.error(f"❌ 获取列表失败: {e}", exc_info=True)
        return

    if not lists_resp.data:
        logger.info("📭 你还没有创建任何列表")
        return

    print("\n" + "=" * 60)
    print("  你创建的所有列表")
    print("=" * 60)
    for lst in lists_resp.data:
        print(f"  列表ID: {lst.id}")
        print(f"  名称:   {lst.name}")
        print("-" * 60)

    logger.info(f"共找到 {len(lists_resp.data)} 个列表，请将需要抓取的列表ID填入 .env 的 X_LIST_ID")


if __name__ == "__main__":
    start_time = datetime.now()
    logger.info(f"🚀 脚本启动时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "lists":
        # 查看所有自建列表: python run_x.py lists
        show_my_all_lists()
        sys.exit(0)

    try:
        result = fetch_list_timeline()
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
