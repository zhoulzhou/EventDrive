import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://www.cls.cn/depth?id=1000', wait_until='networkidle', timeout=60000)
        await asyncio.sleep(3)

        for selector in ['[class*=depth]', '.article-item', '.depth-item', 'article', '.list li', '.item']:
            items = await page.query_selector_all(selector)
            if items:
                print(f'{selector}: {len(items)} items')
                for i, item in enumerate(items[:5], 1):
                    text = await item.inner_text()
                    print(f'  {i}. {text[:60]}...')

        await browser.close()
        print("Done")

asyncio.run(main())
