import os
import sys
import time
import threading
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

test_news = {
    "title": "测试新闻：A股三大指数集体上涨",
    "url": "https://example.com/news/1",
    "publish_time": "2025-01-01 10:00:00",
    "source": "东方财富"
}

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

import app.utils.feishu_notifier as fn
fn.LAST_SEND_TIME = 0.0
fn._draining = False
print("冷却时间已清空")
print(f"当前队列长度: {len(_pending_queue)}")

print()
print("=" * 50)
print("测试各渠道飞书推送...")
print("=" * 50)

results = []

def test_notify(name, func, *args):
    print(f"\n📰 测试 {name}")
    try:
        result = func(*args)
        print(f"   返回: {'✅ 成功' if result else '❌ 失败/缓存'}")
        results.append((name, result))
    except Exception as e:
        print(f"   ❌ 异常: {e}")
        results.append((name, False))

# 测试各渠道
test_notify("dfcf_feishu_notify (东方财富)", dfcf_feishu_notify, [test_news], "东方财富")
time.sleep(1)

test_notify("cls_feishu_notify (财联社)", cls_feishu_notify, [test_news], "财联社")
time.sleep(1)

test_notify("nyt_feishu_notify (纽约时报)", nyt_feishu_notify, [test_news], "纽约时报")
time.sleep(1)

test_notify("bbc_feishu_notify (BBC)", bbc_feishu_notify, [test_news], "BBC")
time.sleep(1)

analysis_result = """### 1. 对宏观投资环境的影响
- 影响类型：政策
- 影响方向：正面
- 影响强度：中
- 核心逻辑：政策暖风提振市场风险偏好

### 2. 对整体股票市场的影响
- 影响市场：A股
- 影响方向：利好
- 影响范围：全局
- 传导机制：权重股拉升指数

### 3. 对相关上市公司的影响
#### 受益公司（正面）
1. 公司：券商板块
   - 逻辑：市场活跃度提升
   - 周期：短期

#### 受损公司（负面）
1. 公司：空头资金
   - 逻辑：指数上涨压缩做空空间
   - 周期：短期

### 4. 投资操作建议（明确可执行）
- 整体仓位：加仓
- 行业配置：超配金融
- 个股操作：
  - 买入/增持：东方财富、中信证券
  - 卖出/减仓：空头仓位
  - 持有/观望：观望
- 关键风险提示：
  1. 政策效果不及预期
  2. 海外市场波动传导
  3. 流动性紧张"""

test_notify("doubao_feishu_notify (豆包分析)", doubao_feishu_notify, test_news["title"], analysis_result, "东方财富")
time.sleep(1)

test_notify("openrouter_feishu_notify (OpenRouter分析)", openrouter_feishu_notify, test_news["title"], analysis_result, "财联社")

print()
print("=" * 50)
print(f"测试完成! 队列中还有 {len(_pending_queue)} 条待发送")
print("=" * 50)
print("\n等待缓存消息发送（5秒）...")
time.sleep(5)
print(f"最终队列长度: {len(_pending_queue)}")
print()
print("✅ 全部测试完成! 请检查各飞书群消息是否收到。")
