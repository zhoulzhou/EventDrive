import httpx
import asyncio
import json
import re

async def get_cls_depth():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, verify=False) as client:
        print("获取财联社深度页面...")

        r = await client.get("https://www.cls.cn/depth?id=1000", headers=headers)
        print(f"Status: {r.status_code}, Length: {len(r.text)}")

        if '__NEXT_DATA__' in r.text:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', r.text)
            if match:
                next_data = json.loads(match.group(1))
                print("\n找到Next.js数据!")

                initial_state = next_data.get('props', {}).get('initialState', {})
                depth_data = initial_state.get('depth', {})

                print(f"depth keys: {list(depth_data.keys())}")

                for key in depth_data.keys():
                    val = depth_data[key]
                    if isinstance(val, dict) and val:
                        print(f"\n{key}: {list(val.keys())[:10]}")

                depth_article = depth_data.get('depthArticleData', {})
                if depth_article:
                    print(f"\n找到depthArticleData!")
                    print(json.dumps(depth_article, ensure_ascii=False)[:500])

                role = depth_data.get('role', {})
                if role:
                    print(f"\nrole: {role}")

                print("\n尝试获取Telegraph:")
                telegraph = initial_state.get('telegraph', {})
                print(f"telegraph keys: {list(telegraph.keys())}")
                telegraph_list = telegraph.get('telegraphList', [])
                if telegraph_list:
                    print(f"telegraphList: {len(telegraph_list)} items")

        print("\n\n尝试直接访问API...")
        api_urls = [
            ("depth", "https://www.cls.cn/v1/depth?subject_id=1000&page=1&rn=5"),
            ("depth2", "https://www.cls.cn/v1/depth?subject_id=1003&page=1&rn=5"),
            ("telegraph", "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&rn=5&sv=7.7.5"),
            ("subject", "https://www.cls.cn/nodeapi/getSubjectData?app=CailianpressWeb&os=web&rn=5&sv=7.7.5&subject_id=1000"),
        ]

        for name, url in api_urls:
            try:
                r2 = await client.get(url, headers={**headers, "Referer": "https://www.cls.cn/"})
                if r2.status_code == 200:
                    try:
                        data = r2.json()
                        if data.get('error') == 0 or data.get('errno') == 0 or data.get('code') == 0:
                            print(f"\n✅ {name}: 成功")
                            if 'data' in data:
                                d = data['data']
                                if isinstance(d, dict):
                                    print(f"   keys: {list(d.keys())}")
                                    if 'roll_data' in d and d['roll_data']:
                                        print(f"   roll_data: {len(d['roll_data'])} items")
                                        for i, item in enumerate(d['roll_data'][:3], 1):
                                            print(f"     {i}. {item.get('title', '')[:40] or item.get('content', '')[:40]}")
                                    elif 'list' in d and d['list']:
                                        print(f"   list: {len(d['list'])} items")
                                        for i, item in enumerate(d['list'][:3], 1):
                                            print(f"     {i}. {item.get('title', '')[:40]}")
                                    elif 'articles' in d and d['articles']:
                                        print(f"   articles: {len(d['articles'])} items")
                        elif 'errno' in data:
                            print(f"\n❌ {name}: {data.get('msg', data.get('errno'))}")
                    except:
                        pass
            except Exception as e:
                print(f"{name}: Error - {e}")

asyncio.run(get_cls_depth())
