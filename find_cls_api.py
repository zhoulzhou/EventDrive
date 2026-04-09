import httpx
import asyncio
import re

async def find_cls_headlines_api():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        print("访问财联社首页...")
        response = await client.get("https://www.cls.cn/", headers=headers)
        print(f"Status: {response.status_code}")
        print(f"Content length: {len(response.text)}")

        api_patterns = [
            r'["\']([^"\']*nodeapi[^"\']*)["\']',
            r'["\']([^"\']*api[^"\']*headline[^"\']*)["\']',
            r'["\']([^"\']*api[^"\']*essential[^"\']*)["\']',
            r'["\']([^"\']*api[^"\']* CLS [^"\']*)["\']',
            r'window\.__NUXT__\s*=\s*({.*?});',
        ]

        for pattern in api_patterns:
            matches = re.findall(pattern, response.text, re.DOTALL | re.IGNORECASE)
            if matches:
                print(f"\nFound pattern: {pattern[:50]}...")
                for m in matches[:5]:
                    if len(m) > 20 and len(m) < 300:
                        print(f"  - {m[:150]}")

        headlines_pattern = r'(cls\.cn/nodeapi/[^"\'&\s]+)'
        api_urls = re.findall(headlines_pattern, response.text)
        if api_urls:
            print(f"\nFound API URLs: {list(set(api_urls))[:10]}")

asyncio.run(find_cls_headlines_api())
