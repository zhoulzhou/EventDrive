"""
获取 X 平台用户数字 UID 测试脚本
- 使用纯手写 OAuth1 HMAC-SHA1 签名
- 接口: GET /2/users/me
- 从 .env 读取配置
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
from urllib.parse import quote_plus
from dotenv import load_dotenv
import requests

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("get_x_id")

# ===================== 密钥配置 =====================
CK = os.getenv("X_CONSUMER_KEY", "")
CS = os.getenv("X_CONSUMER_SECRET", "")
AT = os.getenv("X_ACCESS_TOKEN", "")
ATS = os.getenv("X_ACCESS_TOKEN_SECRET", "")
# ===================================================


def get_auth_header(method, url, params):
    """手动构造标准 OAuth1 Authorization Header (RFC5849)"""
    nonce = base64.b64encode(os.urandom(32)).decode().replace("+", "").replace("/", "").replace("=", "")
    ts = str(int(time.time()))

    logger.debug(f"[OAuth] nonce: {nonce[:20]}...")
    logger.debug(f"[OAuth] timestamp: {ts}")

    oauth_base = {
        "oauth_consumer_key": CK,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts,
        "oauth_token": AT,
        "oauth_version": "1.0"
    }
    all_params = {**oauth_base, **params}
    sorted_pairs = sorted(all_params.items())
    param_join = "&".join(f"{quote_plus(k)}={quote_plus(str(v))}" for k, v in sorted_pairs)
    base_str = f"{method}&{quote_plus(url)}&{quote_plus(param_join)}"

    logger.debug(f"[OAuth] signature base string (前150字): {base_str[:150]}...")

    sign_key = f"{quote_plus(CS)}&{quote_plus(ATS)}"
    raw_sig = hmac.new(sign_key.encode(), base_str.encode(), hashlib.sha1).digest()
    sig = base64.b64encode(raw_sig).decode()

    logger.debug(f"[OAuth] signature: {sig}")

    parts = [
        f'oauth_consumer_key="{quote_plus(CK)}"',
        f'oauth_nonce="{quote_plus(nonce)}"',
        f'oauth_signature="{quote_plus(sig)}"',
        'oauth_signature_method="HMAC-SHA1"',
        f'oauth_timestamp="{ts}"',
        f'oauth_token="{quote_plus(AT)}"',
        'oauth_version="1.0"'
    ]
    auth_header = f"OAuth {', '.join(parts)}"
    logger.debug(f"[OAuth] Authorization header (前100字): {auth_header[:100]}...")
    return auth_header


def main():
    print("=" * 60)
    print("  X 平台用户数字 UID 获取工具")
    print("=" * 60)

    # 检查配置
    logger.info("[配置] 检查 API 凭证...")
    if not all([CK, CS, AT, ATS]):
        logger.error("❌ [配置] 缺少 API 凭证，请在 .env 中配置 X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET")
        sys.exit(1)

    logger.info(f"[配置] CONSUMER_KEY: {CK[:8]}... (已隐藏)")
    logger.info(f"[配置] ACCESS_TOKEN: {AT[:8]}... (已隐藏)")

    # 获取数字UID
    url = "https://api.x.com/2/users/me"
    logger.info(f"\n🌐 请求 URL: {url}")
    logger.info("🌐 正在构造 OAuth1 签名...")

    try:
        header = {"Authorization": get_auth_header("GET", url, {})}
        logger.info("🌐 Authorization header 构造完成")

        logger.info("🌐 正在发送 GET 请求...")
        res = requests.get(url, headers=header, timeout=30)
        logger.info(f"🌐 响应状态码: {res.status_code}")
    except Exception as e:
        logger.error(f"❌ 请求异常: {e}", exc_info=True)
        sys.exit(1)

    try:
        res_json = res.json()
    except Exception as e:
        logger.error(f"❌ JSON 解析失败: {e}")
        logger.error(f"❌ 响应内容: {res.text[:500]}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  响应结果")
    print("=" * 60)
    print(json.dumps(res_json, indent=2, ensure_ascii=False))

    if res.status_code == 200 and "data" in res_json:
        user_data = res_json["data"]
        user_id = user_data.get("id", "")
        username = user_data.get("username", "")
        name = user_data.get("name", "")

        print("\n" + "=" * 60)
        print("  用户信息")
        print("=" * 60)
        print(f"  数字 UID:  {user_id}")
        print(f"  用户名:    @{username}")
        print(f"  显示名称:  {name}")
        print("=" * 60)

        logger.info(f"\n✅ 获取成功！用户数字 UID: {user_id}")
        return user_id
    else:
        logger.error(f"\n❌ 获取失败: {res_json}")
        sys.exit(1)


if __name__ == "__main__":
    main()
