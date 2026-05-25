#!/usr/bin/env python3
"""飞书互动助手测试脚本

测试内容:
  1. AI 大模型连通性
  2. 飞书 API Token 获取
  3. 飞书 API 调用 (获取机器人信息)
  4. 检查事件订阅配置
  5. 直接发送测试消息验证消息发送能力

用法: python3 test_feishu_bot.py
"""

import os
import sys
import json
import requests

# 项目根目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

OK = "✅"
FAIL = "❌"
WARN = "⚠️ "


def test_ai_model():
    """测试 AI 大模型连通性"""
    print(f"\n[1] 测试 AI 大模型连通性...")
    try:
        from bot.analyzer_factory import AnalyzerFactory
        analyzer = AnalyzerFactory.create()
        model_name = type(analyzer).__name__
        print(f"    模型类型: {model_name}")

        reply = analyzer.chat("你好，请用一句话介绍自己", "你是测试助手")
        print(f"    AI 回复: {reply[:150]}")
        print(f"    {OK} AI 模型测试通过")
        return True
    except Exception as e:
        print(f"    {FAIL} AI 模型测试失败: {e}")
        return False


def test_feishu_token():
    """测试飞书 API Token 获取"""
    print(f"\n[2] 测试飞书 API Token...")
    app_id = os.getenv("BOT_FEISHU_APP_ID", "")
    app_secret = os.getenv("BOT_FEISHU_APP_SECRET", "")

    if not app_id or app_id == "cli_xxx":
        print(f"    {FAIL} BOT_FEISHU_APP_ID 未配置")
        return None

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            print(f"    {OK} Token 获取成功")
            return token
        else:
            print(f"    {FAIL} Token 获取失败: code={data.get('code')}, msg={data.get('msg')}")
            return None
    except Exception as e:
        print(f"    {FAIL} 请求异常: {e}")
        return None


def test_feishu_bot_info(token):
    """测试获取机器人信息"""
    print(f"\n[3] 测试获取机器人信息...")
    try:
        resp = requests.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            bot = data.get("data", {}).get("bot", {})
            print(f"    机器人名称: {bot.get('app_name', 'unknown')}")
            print(f"    {OK} 机器人信息获取成功")
            return True
        else:
            print(f"    {FAIL} 获取失败: code={data.get('code')}, msg={data.get('msg')}")
            if data.get("code") == 99991663:
                print(f"    {WARN} 错误码 99991663 = 应用未开通机器人能力，请到飞书开放平台 -> 应用功能 -> 添加机器人")
            return False
    except Exception as e:
        print(f"    {FAIL} 请求异常: {e}")
        return False


def test_event_subscriptions(token):
    """检查事件订阅配置"""
    print(f"\n[4] 检查事件订阅配置...")
    try:
        app_id = os.getenv("BOT_FEISHU_APP_ID", "")

        # 获取应用的事件订阅列表
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/event/v1/app/event_subscription/list",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            subscriptions = data.get("data", {}).get("event_types", [])
            if subscriptions:
                print(f"    已订阅事件: {subscriptions}")
                has_msg = any("im.message.receive_v1" in s for s in subscriptions)
                if has_msg:
                    print(f"    {OK} 已订阅 im.message.receive_v1 事件")
                else:
                    print(f"    {WARN} 未订阅 im.message.receive_v1 事件")
                    print(f"    请在飞书开放平台 -> 事件订阅 -> 添加事件 -> 选择: 接收消息 v2.0")
            else:
                print(f"    {WARN} 没有订阅任何事件")
                print(f"    请在飞书开放平台 -> 事件订阅 -> 添加事件 -> 选择: 接收消息 v2.0")
        else:
            print(f"    {WARN} 获取订阅列表失败: code={data.get('code')}")
            print(f"    请手动检查: 飞书开放平台 -> {app_id[:16]}... -> 事件订阅")
    except Exception as e:
        print(f"    {WARN} 请求异常: {e}")


def test_send_message(token):
    """测试直接发送消息（验证消息发送能力）"""
    print(f"\n[5] 测试消息发送能力...")
    print(f"    此测试需要指定接收者 open_id")
    print(f"    可通过飞书开发者后台 -> 应用 -> 成员列表 获取 open_id")

    target_id = os.getenv("TEST_CHAT_ID", "")
    if not target_id:
        print(f"    {WARN} 跳过 (未设置 TEST_CHAT_ID)")
        print(f"    如需测试: export TEST_CHAT_ID=ou_xxx 然后重新运行")
        return False

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": target_id,
                "msg_type": "text",
                "content": json.dumps({"text": "🤖 飞书互动助手测试消息 - 如果你看到这条消息，说明消息发送功能正常"}),
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            print(f"    {OK} 测试消息发送成功, msg_id={data['data']['message_id']}")
            return True
        else:
            print(f"    {FAIL} 发送失败: code={data.get('code')}, msg={data.get('msg')}")
            return False
    except Exception as e:
        print(f"    {FAIL} 请求异常: {e}")
        return False


def print_diagnosis():
    """打印诊断信息"""
    print(f"\n{'='*60}")
    print(f"  诊断总结")
    print(f"{'='*60}")

    app_id = os.getenv("BOT_FEISHU_APP_ID", "")
    ai_model = os.getenv("BOT_AI_MODEL", "未设置")

    print(f"  模型类型:     {ai_model}")
    print(f"  飞书 App ID:  {app_id[:20]}...")

    print(f"")
    print(f"  如果所有测试通过但发消息仍无回复，请检查:")
    print(f"  1. 飞书开放平台 -> 事件订阅 -> 确保已添加以下事件:")
    print(f"     im.message.receive_v1 (接收消息 v2.0)")
    print(f"  2. 飞书开放平台 -> 权限管理 -> 确保已开通:")
    print(f"     im:message (获取与发送单聊、群组消息)")
    print(f"  3. 飞书开放平台 -> 应用功能 -> 确保已添加「机器人」能力")
    print(f"  4. 应用必须已发布并通过审核 (企业自建应用需要管理员审批)")
    print(f"  5. 在飞书中搜索机器人名称，发起私聊")
    print(f"")
    print(f"  查看实时日志: tail -f logs/bot.log")
    print(f"{'='*60}")


def main():
    print("=" * 60)
    print("  飞书互动助手 诊断测试")
    print("=" * 60)

    # AI 模型测试
    ai_ok = test_ai_model()

    # 飞书 Token 测试
    token = test_feishu_token()
    if not token:
        print(f"\n{FAIL} 飞书 Token 获取失败，跳过后续测试")
        print_diagnosis()
        sys.exit(1)

    # 机器人信息
    test_feishu_bot_info(token)

    # 事件订阅
    test_event_subscriptions(token)

    # 消息发送测试
    test_send_message(token)

    # 诊断总结
    print_diagnosis()


if __name__ == "__main__":
    main()