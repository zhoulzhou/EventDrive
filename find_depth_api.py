import httpx
import asyncio
import json
import re

async def find_depth_api():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.cls.cn",
    }

    base_url = "https://www.cls.cn"

    common_apis = [
        f"{base_url}/nodeapi/depth?subject_id=1000&page=1&rn=5",
        f"{base_url}/nodeapi/depthList?subject_id=1000&page=1&rn=5",
        f"{base_url}/nodeapi/depth/list?subject_id=1000&page=1&rn=5",
        f"{base_url}/nodeapi/article/depth?subject_id=1000&page=1&rn=5",
        f"{base_url}/api/depth?subject_id=1000&page=1&rn=5",
        f"{base_url}/api/depthList?subject_id=1000&page=1&rn=5",
    ]

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for url in common_apis:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200 and 'json' in r.headers.get('Content-Type', ''):
                    data = r.json()
                    if data.get('code') == 0 or data.get('error') == 0:
                        print(f"✅ {url}")
                        print(f"   data keys: {list(data.get('data', {}).keys())}")
                    else:
                        print(f"❌ {url} - code: {data.get('code')}")
                else:
                    print(f"❌ {url} - status: {r.status_code}")
            except Exception as e:
                print(f"❌ {url} - {e}")

    test_url = f"{base_url}/nodeapi/depth?subject_id=1000&page=1&rn=5"
    print(f"\n详细测试: {test_url}")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(test_url, headers=headers)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            print(f"Response: {r.text[:500]}")

asyncio.run(find_depth_api())
