import httpx
import asyncio
import json

async def try_browser_style():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
        print("获取深度页面...")
        r = await client.get("https://www.cls.cn/depth", headers=headers)
        print(f"Status: {r.status_code}")

        if '__NEXT_DATA__' in r.text:
            import re
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', r.text)
            if match:
                data = json.loads(match.group(1))
                print("\nNext.js data found!")
                print(f"Keys: {list(data.keys())}")

                initial_state = data.get('props', {}).get('initialState', {})
                depth_data = initial_state.get('depth', {})
                print(f"Depth keys: {list(depth_data.keys())}")

                depth_articles = depth_data.get('depthArticleData', {})
                if depth_articles:
                    print(f"depthArticleData: {json.dumps(depth_articles, ensure_ascii=False)[:300]}")

        print("\n尝试其他API...")
        test_urls = [
            "https://www.cls.cn/ajax/depthList?subject_id=1000&page=1&rn=5",
            "https://www.cls.cn/v1/depth?subject_id=1000&page=1&rn=5",
            "https://www.cls.cn/api/v1/depth?subject_id=1000&page=1&rn=5",
            "https://www.cls.cn/api/front/depth?subject_id=1000&page=1&rn=5",
        ]

        for url in test_urls:
            try:
                r2 = await client.get(url, headers=headers)
                print(f"{url}: {r2.status_code}")
            except Exception as e:
                print(f"{url}: Error - {e}")

asyncio.run(try_browser_style())
