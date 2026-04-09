import httpx
import asyncio
import json

async def try_more_approaches():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.cls.cn/",
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, verify=False) as client:
        apis = [
            ("homeData", "https://www.cls.cn/nodeapi/homeData?app=CailianpressWeb&os=web&rn=5&sv=7.7.5"),
            ("index", "https://www.cls.cn/nodeapi/index?app=CailianpressWeb&rn=5&sv=7.7.5"),
            ("hotArticle", "https://www.cls.cn/nodeapi/hotArticle?app=CailianpressWeb&os=web&rn=5&sv=7.7.5"),
            ("recommend", "https://www.cls.cn/nodeapi/recommend?app=CailianpressWeb&os=web&rn=5&sv=7.7.5"),
            ("vipArticle", "https://www.cls.cn/nodeapi/vipArticle?app=CailianpressWeb&os=web&rn=5&sv=7.7.5"),
            ("telegraphWithTime", "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&rn=10&sv=7.7.5&level=&rc=1&tag="),
            ("depthPage", "https://www.cls.cn/api/depth?id=1000"),
        ]

        for name, url in apis:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    ct = r.headers.get('Content-Type', '')
                    if 'json' in ct or 'javascript' in ct:
                        data = r.json()
                        code = data.get('error') or data.get('errno') or data.get('code')
                        if code == 0 or code is None:
                            print(f"\n✅ {name}: 成功")
                            content = data.get('data', {})
                            if isinstance(content, dict):
                                for k, v in content.items():
                                    if v and isinstance(v, list):
                                        print(f"   {k}: {len(v)} items")
                                        if v and len(v) > 0:
                                            first = v[0]
                                            if isinstance(first, dict):
                                                title = first.get('title', first.get('content', '')[:40])
                                                print(f"      First: {str(title)[:60]}")
                        else:
                            print(f"\n❌ {name}: {data.get('msg', data.get('message', 'error'))}")
                    else:
                        print(f"\n❌ {name}: Not JSON ({ct[:30]})")
                else:
                    print(f"\n❌ {name}: status={r.status_code}")
            except Exception as e:
                print(f"\n❌ {name}: {e}")

asyncio.run(try_more_approaches())
