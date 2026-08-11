import random
import asyncio
import logging
import time
from typing import Dict, Optional
from app.config import settings

_delay_logger = logging.getLogger("anti_crawl.delay")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_random_headers(referer: str = None) -> Dict[str, str]:
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0"
    }
    if referer:
        headers["Referer"] = referer
    return headers


def random_delay(min_delay: int = None, max_delay: int = None, source: str = "") -> None:
    min_d = min_delay or settings.MIN_DELAY
    max_d = max_delay or settings.MAX_DELAY
    delay = random.uniform(min_d, max_d)
    tag = f"[{source}] " if source else ""
    _delay_logger.debug(f"{tag}sync_delay start: {delay:.2f}s")
    time.sleep(delay)
    _delay_logger.debug(f"{tag}sync_delay done: {delay:.2f}s")


async def async_random_delay(min_delay: int = None, max_delay: int = None, source: str = "") -> None:
    min_d = min_delay or settings.MIN_DELAY
    max_d = max_delay or settings.MAX_DELAY
    delay = random.uniform(min_d, max_d)
    tag = f"[{source}] " if source else ""
    loop = asyncio.get_event_loop()
    start = loop.time()
    pending = len(asyncio.all_tasks(loop))
    _delay_logger.debug(f"{tag}async_delay start: {delay:.2f}s (pending_tasks={pending})")
    await asyncio.sleep(delay)
    elapsed = loop.time() - start
    pending_after = len(asyncio.all_tasks(loop))
    _delay_logger.debug(f"{tag}async_delay done: planned={delay:.2f}s actual={elapsed:.2f}s (pending_tasks={pending_after})")
