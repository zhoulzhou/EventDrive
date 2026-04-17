import os
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.utils.feishu_notifier import (
    init_dfcf_feishu_notifier,
    init_cls_feishu_notifier,
    init_nyt_feishu_notifier,
    init_bbc_feishu_notifier,
    init_kb_feishu_notifier,
    init_openrouter_feishu_notifier,
    dfcf_feishu_notify,
    cls_feishu_notify,
    nyt_feishu_notify,
    bbc_feishu_notify,
    doubao_feishu_notify,
    openrouter_feishu_notify,
    _pending_queue,
    LAST_SEND_TIME,
)
import app.utils.feishu_notifier as fn

print("=" * 50)
print("初始化飞书推送...")
print("=" * 50)

if settings.DFCF_FEISHU_WEBHOOK_URL:
    init_dfcf_feishu_notifier(settings.DFCF_FEISHU_WEBHOOK_URL, "", settings.DFCF_FEISHU_KEYWORD)
    print(f"✅ 东方财富: {settings.DFCF_FEISHU_WEBHOOK_URL[:50]}...")

if settings.CLS_FEISHU_WEBHOOK_URL:
    init_cls_feishu_notifier(settings.CLS_FEISHU_WEBHOOK_URL, "", settings.CLS_FEISHU_KEYWORD)
    print(f"✅ 财联社: {settings.CLS_FEISHU_WEBHOOK_URL[:50]}...")

if settings.NYT_FEISHU_WEBHOOK_URL:
    init_nyt_feishu_notifier(settings.NYT_FEISHU_WEBHOOK_URL, "", settings.NYT_FEISHU_KEYWORD)
    print(f"✅ 纽约时报: {settings.NYT_FEISHU_WEBHOOK_URL[:50]}...")

if settings.BBC_FEISHU_WEBHOOK_URL:
    init_bbc_feishu_notifier(settings.BBC_FEISHU_WEBHOOK_URL, "", settings.BBC_FEISHU_KEYWORD)
    print(f"✅ BBC: {settings.BBC_FEISHU_WEBHOOK_URL[:50]}...")

if settings.KB_FEISHU_WEBHOOK_URL:
    init_kb_feishu_notifier(settings.KB_FEISHU_WEBHOOK_URL, "", settings.KB_KEYWORD)
    print(f"✅ 豆包分析: {settings.KB_FEISHU_WEBHOOK_URL[:50]}...")

if settings.OPENROUTER_FEISHU_WEBHOOK_URL:
    init_openrouter_feishu_notifier(settings.OPENROUTER_FEISHU_WEBHOOK_URL, "", settings.OPENROUTER_KEYWORD)
    print(f"✅ OpenRouter分析: {settings.OPENROUTER_FEISHU_WEBHOOK_URL[:50]}...")

print()
print("=" * 50)
print("清空队列和冷却时间...")
print("=" * 50)
_pending_queue.clear()
fn.LAST_SEND_TIME = 0.0
fn._draining = False
print(f"冷却时间已清空，当前队列长度: {len(_pending_queue)}")

analysis_template = """### 1. 对宏观投资环境的影响
- 影响类型：政策
- 影响方向：正面
- 影响强度：中
- 核心逻辑：政策暖风提振市场风险偏好

### 2. 投资操作建议
- 整体仓位：加仓
- 行业配置：超配金融"""

print()
print("=" * 50)
print("开始模拟真实场景测试...")
print("=" * 50)

total_sent = 0

def send_and_wait(name, func, *args, delay=2):
    global total_sent
    result = func(*args)
    queue_len = len(_pending_queue)
    print(f"  [{name}] 发送结果: {'✅' if result else '❌'}, 队列: {queue_len}")
    time.sleep(delay)
    return result

# 第1轮：东方财富
print("\n📰 【东方财富】抓取完成，推送新闻1-5条")
send_and_wait("dfcf#1", dfcf_feishu_notify, [{"title": "东方财富-新闻1：A股三大指数集体上涨", "url": "https://dfcf.com/1", "source": "东方财富"}], "东方财富")
send_and_wait("dfcf#2", dfcf_feishu_notify, [{"title": "东方财富-新闻2：券商板块午后拉升", "url": "https://dfcf.com/2", "source": "东方财富"}], "东方财富")
send_and_wait("dfcf#3", dfcf_feishu_notify, [{"title": "东方财富-新闻3：北向资金净流入超百亿", "url": "https://dfcf.com/3", "source": "东方财富"}], "东方财富")
send_and_wait("dfcf#4", dfcf_feishu_notify, [{"title": "东方财富-新闻4：科创板做市商制度落地", "url": "https://dfcf.com/4", "source": "东方财富"}], "东方财富")
send_and_wait("dfcf#5", dfcf_feishu_notify, [{"title": "东方财富-新闻5：银行理财规模突破30万亿", "url": "https://dfcf.com/5", "source": "东方财富"}], "东方财富")

# 第1轮：豆包分析东方财富新闻
print("\n🧠 【豆包分析】分析东方财富新闻1")
send_and_wait("doubao#1", doubao_feishu_notify, "东方财富-新闻1：A股三大指数集体上涨", analysis_template, "东方财富")
send_and_wait("doubao#2", doubao_feishu_notify, "东方财富-新闻2：券商板块午后拉升", analysis_template, "东方财富")

# 第2轮：财联社
print("\n📰 【财联社】抓取完成，推送新闻1-5条")
send_and_wait("cls#1", cls_feishu_notify, [{"title": "财联社-新闻1：央行开展5000亿MLF操作", "url": "https://cls.com/1", "source": "财联社"}], "财联社")
send_and_wait("cls#2", cls_feishu_notify, [{"title": "财联社-新闻2：多家银行下调存款利率", "url": "https://cls.com/2", "source": "财联社"}], "财联社")
send_and_wait("cls#3", cls_feishu_notify, [{"title": "财联社-新闻3：沪深两市成交额突破万亿", "url": "https://cls.com/3", "source": "财联社"}], "财联社")
send_and_wait("cls#4", cls_feishu_notify, [{"title": "财联社-新闻4：监管层表态支持头部券商并购", "url": "https://cls.com/4", "source": "财联社"}], "财联社")
send_and_wait("cls#5", cls_feishu_notify, [{"title": "财联社-新闻5：公募基金规模再创新高", "url": "https://cls.com/5", "source": "财联社"}], "财联社")

# 第2轮：OpenRouter分析财联社新闻
print("\n🧠 【OpenRouter分析】分析财联社新闻1")
send_and_wait("openrouter#1", openrouter_feishu_notify, "财联社-新闻1：央行开展5000亿MLF操作", analysis_template, "财联社")
send_and_wait("openrouter#2", openrouter_feishu_notify, "财联社-新闻2：多家银行下调存款利率", analysis_template, "财联社")

# 第3轮：纽约时报
print("\n📰 【纽约时报】抓取完成，推送新闻1-5条")
send_and_wait("nyt#1", nyt_feishu_notify, [{"title": "NYT-新闻1：Fed signals potential rate cuts", "url": "https://nyt.com/1", "source": "纽约时报"}], "纽约时报")
send_and_wait("nyt#2", nyt_feishu_notify, [{"title": "NYT-新闻2：US stocks rally on tech earnings", "url": "https://nyt.com/2", "source": "纽约时报"}], "纽约时报")
send_and_wait("nyt#3", nyt_feishu_notify, [{"title": "NYT-新闻3：Oil prices surge amid supply concerns", "url": "https://nyt.com/3", "source": "纽约时报"}], "纽约时报")
send_and_wait("nyt#4", nyt_feishu_notify, [{"title": "NYT-新闻4：China manufacturing data beats estimates", "url": "https://nyt.com/4", "source": "纽约时报"}], "纽约时报")
send_and_wait("nyt#5", nyt_feishu_notify, [{"title": "NYT-新闻5：Global markets mixed on policy outlook", "url": "https://nyt.com/5", "source": "纽约时报"}], "纽约时报")

# 第3轮：豆包分析纽约时报新闻
print("\n🧠 【豆包分析】分析纽约时报新闻1")
send_and_wait("doubao#3", doubao_feishu_notify, "NYT-新闻1：Fed signals potential rate cuts", analysis_template, "纽约时报")

# 第4轮：BBC
print("\n📰 【BBC】抓取完成，推送新闻1-5条")
send_and_wait("bbc#1", bbc_feishu_notify, [{"title": "BBC-新闻1：UK inflation falls to 2-year low", "url": "https://bbc.com/1", "source": "BBC"}], "BBC")
send_and_wait("bbc#2", bbc_feishu_notify, [{"title": "BBC-新闻2：European markets close higher", "url": "https://bbc.com/2", "source": "BBC"}], "BBC")
send_and_wait("bbc#3", bbc_feishu_notify, [{"title": "BBC-新闻3：Tech giants report strong results", "url": "https://bbc.com/3", "source": "BBC"}], "BBC")
send_and_wait("bbc#4", bbc_feishu_notify, [{"title": "BBC-新闻4：Global bond yields decline", "url": "https://bbc.com/4", "source": "BBC"}], "BBC")
send_and_wait("bbc#5", bbc_feishu_notify, [{"title": "BBC-新闻5：Central banks hold rates steady", "url": "https://bbc.com/5", "source": "BBC"}], "BBC")

# 第4轮：OpenRouter分析BBC新闻
print("\n🧠 【OpenRouter分析】分析BBC新闻1")
send_and_wait("openrouter#3", openrouter_feishu_notify, "BBC-新闻1：UK inflation falls to 2-year low", analysis_template, "BBC")

print()
print("=" * 50)
print(f"发送完成! 队列中还有 {len(_pending_queue)} 条待发送")
print("=" * 50)
print("\n等待缓存消息全部发送...")

while len(_pending_queue) > 0:
    time.sleep(2)
    print(f"  队列剩余: {len(_pending_queue)}")

print()
print("=" * 50)
print("✅ 全部测试完成!")
print("=" * 50)
print("请检查各飞书群消息：")
print("  1. 东方财富群 - 5条新闻 + 2条分析")
print("  2. 财联社群 - 5条新闻 + 2条分析")
print("  3. 纽约时报群 - 5条新闻 + 1条分析")
print("  4. BBC群 - 5条新闻 + 1条分析")
print("  5. 豆包分析群 - 3条分析")
print("  6. OpenRouter分析群 - 3条分析")
