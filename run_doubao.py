#!/usr/bin/env python3
"""豆包分析器实时测试 - 直接从 .env 文件解析配置，确保始终读取最新值"""
import sys
import os
import logging
import json
import requests
from pathlib import Path


def load_env_file(env_path: Path) -> dict:
    """手动解析 .env 文件，返回 dict，不受 shell 环境变量干扰"""
    config = {}
    if not env_path.exists():
        print(f"❌ .env 文件不存在: {env_path}")
        sys.exit(1)
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                config[key] = value
                os.environ[key] = value  # 同时写入 os.environ，确保后续代码可用
    return config


# ============================================================
# 1. 直接从 .env 文件解析配置（绕过 shell 环境变量缓存）
# ============================================================
env_path = (Path(__file__).parent / ".env").resolve()
print(f"📁 .env 文件绝对路径: {env_path}")
print(f"   文件是否存在: {env_path.exists()}")

# 直接打印 .env 文件中 KB_MODEL_ID 那一行的原始内容
print("   [DEBUG] .env 文件中 KB_MODEL_ID 相关行原始内容:")
with open(env_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        if "KB_MODEL_ID" in line or "KB_API_KEY" in line:
            print(f"     L{i}: {repr(line.rstrip())}")

env = load_env_file(env_path)
print()

KB_API_KEY = env.get("KB_API_KEY", "")
KB_MODEL_ID = env.get("KB_MODEL_ID", "doubao-1-5-pro-256k-250115")
KB_REGION = env.get("KB_REGION", "cn-beijing")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("run_doubao")

print("=" * 70)
print("  豆包分析器 - 实时配置测试")
print("=" * 70)
print(f"  KB_API_KEY : {'已配置 (' + KB_API_KEY[:8] + '...)' if KB_API_KEY else '❌ 未配置!'}")
print(f"  KB_MODEL_ID: {KB_MODEL_ID}")
print(f"  KB_REGION  : {KB_REGION}")
print()

if not KB_API_KEY:
    print("❌ KB_API_KEY 未配置，请在 .env 中设置后重试")
    sys.exit(1)

# ============================================================
# 2. 构建 API 请求
# ============================================================
url = f"https://ark.{KB_REGION}.volces.com/api/v3/chat/completions"

news_title = "英伟达要削减AI芯片的内存？黄仁勋：使用要明智巧妙 争取更多供应"
news_content = (
    "英伟达CEO黄仁勋在近期采访中回应了关于AI芯片内存配置的争议。"
    "他表示，内存使用需要更加明智和巧妙，而不是简单地堆砌容量。"
    "黄仁勋强调，通过架构优化和软件协同，可以在有限内存下实现更高效率。"
    "同时他透露，英伟达正在争取更多的先进封装和HBM内存供应，"
    "以满足下一代AI芯片的需求。市场对此反应不一，"
    "部分分析师认为这可能影响英伟达在AI训练市场的竞争力。"
)

prompt = f"""你是专业金融新闻分析师，请对以下新闻进行深度、结构化分析，严格按以下4个维度输出：

【新闻标题】
{news_title}

【新闻摘要】
{news_content}

【分析要求】
1. 核心事件
   - 用一句话概括新闻的核心事实
   - 明确事件发生的时间、主体、关键数据

2. 关键影响
   - 对宏观经济/政策的影响
   - 对相关行业的影响
   - 对资本市场（股市、债市、汇市等）的影响

3. 市场情绪
   - 判断市场整体情绪：乐观/中性/悲观
   - 分析情绪背后的驱动因素
   - 对比历史类似事件的市场反应

4. 风险提示
   - 列出2-3条需要警惕的关键风险
   - 说明每项风险可能触发的条件
   - 给出风险应对建议

【输出格式】严格用以下结构，不要多余文字
### 1. 核心事件
- 事件概括：
- 关键主体：
- 核心数据：

### 2. 关键影响
- 宏观影响：
- 行业影响：
- 资本市场影响：

### 3. 市场情绪
- 整体情绪：
- 驱动因素：
- 历史参考：

### 4. 风险提示
1. 风险一：
   - 触发条件：
   - 应对建议：
2. 风险二：
   - 触发条件：
   - 应对建议：
3. 风险三：
   - 触发条件：
   - 应对建议：
"""

headers = {
    "Authorization": f"Bearer {KB_API_KEY}",
    "Content-Type": "application/json"
}

payload = {
    "model": KB_MODEL_ID,
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0.3,
    "stream": False
}

print("-" * 70)
print(f"🌐 API URL: {url}")
print(f"📦 Model : {KB_MODEL_ID}")
print(f"📝 新闻标题: {news_title}")
print("-" * 70)
print("⏳ 正在发送分析请求...")
print()

# ============================================================
# 3. 发送请求并详细诊断
# ============================================================
try:
    resp = requests.post(url, headers=headers, json=payload, timeout=180)

    print(f"📊 HTTP 状态码: {resp.status_code}")
    print()

    if resp.status_code == 200:
        result = resp.json()
        if "choices" in result and len(result["choices"]) > 0:
            content = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            print("=" * 70)
            print("  ✅ 分析成功!")
            print("=" * 70)
            if usage:
                print(f"  Token 用量: prompt={usage.get('prompt_tokens', '?')}, "
                      f"completion={usage.get('completion_tokens', '?')}, "
                      f"total={usage.get('total_tokens', '?')}")
            print("-" * 70)
            print(content)
            print("-" * 70)
            print()
            print("✅ 测试通过！KB_MODEL_ID 配置正确")
        else:
            print("❌ API 返回了 200 但响应结构异常")
            print(f"   Response: {json.dumps(result, ensure_ascii=False, indent=2)[:1000]}")
            sys.exit(1)
    else:
        print(f"❌ API 请求失败 (HTTP {resp.status_code})")
        print("-" * 70)
        try:
            error_body = resp.json()
            print(f"  错误详情: {json.dumps(error_body, ensure_ascii=False, indent=2)}")
        except Exception:
            print(f"  响应内容: {resp.text[:1000]}")

        print("-" * 70)

        # 诊断建议
        print()
        print("🔍 故障诊断:")
        if resp.status_code == 500:
            print("   → 500 InternalServerError 通常表示:")
            print("     1. endpoint_id 不存在或未正确部署")
            print("     2. endpoint_id 不属于当前 API Key 的账号")
            print("     3. 模型推理服务内部错误（可重试）")
            print()
            print("   💡 建议检查:")
            print("     - 登录火山方舟控制台 → 推理服务")
            print("     - 确认 endpoint ID 状态为「运行中」")
            print("     - 确认 API Key 有该 endpoint 的调用权限")
        elif resp.status_code == 401:
            print("   → API Key 无效或已过期")
        elif resp.status_code == 403:
            print("   → API Key 无权限访问该 endpoint")
        elif resp.status_code == 404:
            print("   → endpoint_id 不存在，检查 KB_MODEL_ID 是否正确")
        elif resp.status_code == 429:
            print("   → 请求频率过高，稍后重试")

        sys.exit(1)

except requests.exceptions.Timeout:
    print("❌ 请求超时（180秒）")
    sys.exit(1)
except requests.exceptions.ConnectionError as e:
    print(f"❌ 网络连接错误: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ 未知错误: {e}")
    sys.exit(1)