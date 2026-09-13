#!/usr/bin/env python3
"""DeepSeek 分析器实时测试 - 复用 app.utils.deepseek_analyzer 的真实分析路径"""
import sys
import os
from pathlib import Path

# 确保能导入项目模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
import json
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("run_deepseek")


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
                os.environ[key] = value  # 同时写入 os.environ
    return config


# ============================================================
# 1. 从 .env 解析配置（绕过 shell 环境变量缓存）
# ============================================================
env_path = (Path(__file__).parent / ".env").resolve()
print(f"📁 .env 文件绝对路径: {env_path}")
print(f"   文件是否存在: {env_path.exists()}")
print("   [DEBUG] .env 文件中 DEEPSEEK 相关行原始内容:")
with open(env_path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        if "DEEPSEEK" in line:
            print(f"     L{i}: {repr(line.rstrip())}")

env = load_env_file(env_path)
print()

DEEPSEEK_API_KEY = env.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = env.get("DEEPSEEK_MODEL", "deepseek-flash")

print("=" * 70)
print("  DeepSeek 分析器 - 实时配置测试 (复用真实 analyze_news)")
print("=" * 70)
print(f"  DEEPSEEK_API_KEY: {'已配置 (' + DEEPSEEK_API_KEY[:8] + '...)' if DEEPSEEK_API_KEY else '❌ 未配置!'}")
print(f"  DEEPSEEK_MODEL  : {DEEPSEEK_MODEL}")
print()

if not DEEPSEEK_API_KEY:
    print("❌ DEEPSEEK_API_KEY 未配置，请在 .env 中设置后重试")
    sys.exit(1)

# ============================================================
# 2. 复用真实分析器，像新闻分析一样调用
# ============================================================
from app.utils.deepseek_analyzer import DeepSeekAnalyzer, init_deepseek_analyzer, get_deepseek_analyzer

analyzer = DeepSeekAnalyzer(
    api_key=DEEPSEEK_API_KEY,
    model=DEEPSEEK_MODEL,
    feishu_webhook_url=env.get("DEEPSEEK_FEISHU_WEBHOOK_URL", ""),
    keyword=env.get("DEEPSEEK_KEYWORD", "深度分析"),
)
# 同时注册到全局单例，保持与 scheduler 的全等路径
init_deepseek_analyzer(
    api_key=DEEPSEEK_API_KEY,
    model=DEEPSEEK_MODEL,
    feishu_webhook_url=env.get("DEEPSEEK_FEISHU_WEBHOOK_URL", ""),
    keyword=env.get("DEEPSEEK_KEYWORD", "深度分析"),
)
analyzer = get_deepseek_analyzer() or analyzer

news_title = "英伟达要削减AI芯片的内存？黄仁勋：使用要明智巧妙 争取更多供应"
news_content = (
    "英伟达CEO黄仁勋在近期采访中回应了关于AI芯片内存配置的争议。"
    "他表示，内存使用需要更加明智和巧妙，而不是简单地堆砌容量。"
    "黄仁勋强调，通过架构优化和软件协同，可以在有限内存下实现更高效率。"
    "同时他透露，英伟达正在争取更多的先进封装和HBM内存供应，"
    "以满足下一代AI芯片的需求。市场对此反应不一，"
    "部分分析师认为这可能影响英伟达在AI训练市场的竞争力。"
)

print("-" * 70)
print(f"🌐 API Base URL: {analyzer.base_url}")
print(f"📦 Model       : {analyzer.model}")
print(f"🏷  Keyword     : {analyzer.keyword}")
print(f"📝 新闻标题    : {news_title}")
print("-" * 70)
print("⏳ 正在通过 DeepSeekAnalyzer.analyze_news 发送分析请求...")
print()


async def main():
    result = await analyzer.analyze_news(news_content, news_title)

    print()
    if result:
        print("=" * 70)
        print("  ✅ 分析成功!")
        print("=" * 70)
        print(result)
        print("=" * 70)
        print()
        print("✅ 测试通过！DEEPSEEK_MODEL 配置正确，可正常分析")
        return 0
    else:
        print("❌ analyze_news 返回 None，分析失败")
        print()
        print("🔍 故障诊断:")
        print("   - 上面日志中如出现「分析失败 40x」表示模型名无效 / Key 无效")
        print("   - 「分析失败 429」表示请求频率受限")
        print("   - 「API 响应结构错误」表示返回格式异常")
        print("   - 如无任何请求相关日志，请检查网络是否可访问 api.deepseek.com")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)