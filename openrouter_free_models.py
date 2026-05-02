"""
OpenRouter 免费模型获取与可用性测试脚本

用法: python test_openrouter_free_models.py

功能:
1. 调用 OpenRouter /api/v1/models 获取所有模型
2. 筛选出免费模型 (pricing 为 "0")
3. 对每个免费模型发送测试请求，验证其可用性
4. 输出测试结果汇总

参考: https://openrouter.ai/docs/api-reference/models
"""
import os
import sys
import json
import time
import logging
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_openrouter_free")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

if not OPENROUTER_API_KEY:
    print("❌ 未配置 OPENROUTER_API_KEY，请在 .env 中设置")
    sys.exit(1)


def fetch_all_models() -> list:
    """获取 OpenRouter 所有可用模型"""
    print("=" * 70)
    print("📡 正在获取 OpenRouter 模型列表...")
    print("=" * 70)

    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            models = data
        elif isinstance(data, dict) and "data" in data:
            models = data["data"]
        else:
            models = []

        print(f"  ✅ 成功获取 {len(models)} 个模型")
        return models
    except Exception as e:
        print(f"  ❌ 获取模型列表失败: {e}")
        return []


def filter_free_models(all_models: list) -> list:
    """筛选出免费模型"""
    free_models = []

    for model in all_models:
        model_id = model.get("id", "")
        model_name = model.get("name", model_id)

        pricing = model.get("pricing", {})
        prompt_price = pricing.get("prompt", "0")
        completion_price = pricing.get("completion", "0")

        prompt_num = float(prompt_price) if isinstance(prompt_price, str) else prompt_price
        completion_num = float(completion_price) if isinstance(completion_price, str) else completion_price

        if prompt_num == 0 and completion_num == 0:
            free_models.append({
                "id": model_id,
                "name": model_name,
                "context_length": model.get("context_length", 0),
                "description": model.get("description", "")[:80],
                "pricing": pricing
            })

    print(f"  🆓 其中免费模型: {len(free_models)} 个")
    return free_models


def test_model(model_id: str, model_name: str, timeout: int = 15) -> dict:
    """测试单个模型是否可用"""
    result = {
        "id": model_id,
        "name": model_name,
        "status": "unknown",
        "error": "",
        "response": "",
        "latency_ms": 0
    }

    start = time.time()

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "FreeModelTest"
            },
            json={
                "model": model_id,
                "messages": [
                    {"role": "user", "content": "Reply with just 'OK'"}
                ],
                "max_tokens": 10,
                "temperature": 0
            },
            timeout=timeout
        )

        result["latency_ms"] = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                msg = data["choices"][0].get("message", {}).get("content", "")
                if msg is None:
                    msg = ""
                result["status"] = "success"
                result["response"] = msg.strip()[:50]
            else:
                result["status"] = "empty_response"
                result["error"] = "无 choices"
        elif resp.status_code == 402:
            result["status"] = "requires_payment"
            result["error"] = "需要付费"
        elif resp.status_code == 429:
            result["status"] = "rate_limited"
            result["error"] = "请求频率限制"
        elif resp.status_code == 401:
            result["status"] = "unauthorized"
            result["error"] = "认证失败"
        else:
            result["status"] = "failed"
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:100]}"

    except requests.Timeout:
        result["status"] = "timeout"
        result["error"] = f"超时 ({timeout}s)"
        result["latency_ms"] = timeout * 1000
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:100]
        result["latency_ms"] = int((time.time() - start) * 1000)

    return result


def run_all_tests(free_models: list, test_limit: int = 20) -> list:
    """对所有免费模型进行测试"""
    results = []

    to_test = free_models[:test_limit] if len(free_models) > test_limit else free_models

    print(f"\n{'=' * 70}")
    print(f"🧪 开始测试 {len(to_test)} 个免费模型 (最多测试 {test_limit} 个)")
    print(f"{'=' * 70}")

    for i, model in enumerate(to_test):
        mid = model["id"]
        mname = model["name"]
        print(f"\n  [{i + 1}/{len(to_test)}] 测试: {mname}")
        print(f"         ID: {mid}")

        result = test_model(mid, mname)

        icon = {
            "success": "✅",
            "empty_response": "⚠️ ",
            "requires_payment": "💲",
            "rate_limited": "⏳",
            "timeout": "⏱️ ",
            "unauthorized": "🔒",
            "failed": "❌",
            "error": "💥"
        }.get(result["status"], "❓")

        status_text = {
            "success": "可用",
            "empty_response": "空响应",
            "requires_payment": "实际需付费",
            "rate_limited": "频率限制",
            "timeout": "超时",
            "unauthorized": "认证失败",
            "failed": "请求失败",
            "error": "异常"
        }.get(result["status"], result["status"])

        print(f"         {icon} {status_text} | 延迟: {result['latency_ms']}ms")
        if result["response"]:
            print(f"         回复: {result['response']}")
        if result["error"]:
            print(f"         错误: {result['error']}")

        results.append(result)
        time.sleep(0.5)

    return results


def print_summary(free_models: list, test_results: list):
    """打印汇总报告"""
    print(f"\n\n{'=' * 70}")
    print("📊 汇总报告")
    print(f"{'=' * 70}")

    print(f"\n  🆓 OpenRouter 免费模型总数: {len(free_models)}")

    status_count = {}
    for r in test_results:
        s = r["status"]
        status_count[s] = status_count.get(s, 0) + 1

    print(f"\n  📋 测试结果统计 ({len(test_results)} 个模型):")
    print(f"     ✅ 可用        : {status_count.get('success', 0)}")
    print(f"     💲 实际需付费  : {status_count.get('requires_payment', 0)}")
    print(f"     ⏳ 频率限制    : {status_count.get('rate_limited', 0)}")
    print(f"     ⏱️  超时        : {status_count.get('timeout', 0)}")
    print(f"     ❌ 请求失败    : {status_count.get('failed', 0)}")
    print(f"     ⚠️  空响应     : {status_count.get('empty_response', 0)}")
    print(f"     💥 异常        : {status_count.get('error', 0)}")

    available = [r for r in test_results if r["status"] == "success"]
    print(f"\n  📝 可用的免费模型 ({len(available)} 个):")
    print(f"     {'序号':<4} {'可用模型名称':<45} {'延迟':<10} {'模型ID'}")
    print(f"     {'-' * 4} {'-' * 45} {'-' * 10} {'-' * 40}")
    for i, r in enumerate(available, 1):
        name = r["name"][:43]
        print(f"     {i:<4} {name:<45} {str(r['latency_ms']) + 'ms':<10} {r['id']}")

    not_available = [r for r in test_results if r["status"] != "success"]
    if not_available:
        print(f"\n  ⚠️  不可用的免费模型 ({len(not_available)} 个):")
        print(f"     {'序号':<4} {'模型名称':<45} {'状态':<16} {'模型ID'}")
        print(f"     {'-' * 4} {'-' * 45} {'-' * 16} {'-' * 40}")
        status_labels = {
            "requires_payment": "实际需付费",
            "rate_limited": "频率限制",
            "timeout": "超时",
            "failed": "请求失败",
            "empty_response": "空响应",
            "error": "异常",
            "unauthorized": "认证失败"
        }
        for i, r in enumerate(not_available, 1):
            name = r["name"][:43]
            label = status_labels.get(r["status"], r["status"])
            print(f"     {i:<4} {name:<45} {label:<16} {r['id']}")

    print(f"\n{'=' * 70}")
    print(f"  🎉 测试完成! {len(available)}/{len(test_results)} 个免费模型可用")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    print("\n🚀 OpenRouter 免费模型获取与可用性测试")
    print()

    all_models = fetch_all_models()
    if not all_models:
        print("❌ 无法获取模型列表，退出")
        sys.exit(1)

    free_models = filter_free_models(all_models)

    if not free_models:
        print("⚠️  未发现任何免费模型")
        sys.exit(0)

    print("\n🆓 免费模型列表:")
    print(f"   {'序号':<4} {'模型名称':<45} {'上下文长度':<12} {'描述'}")
    print(f"   {'-' * 4} {'-' * 45} {'-' * 12} {'-' * 30}")
    for i, m in enumerate(free_models, 1):
        name = m["name"][:43]
        ctx = str(m.get("context_length", "?"))
        desc = m.get("description", "")[:28]
        print(f"   {i:<4} {name:<45} {ctx:<12} {desc}")

    test_results = run_all_tests(free_models)
    print_summary(free_models, test_results)
