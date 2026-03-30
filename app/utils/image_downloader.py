import hashlib
import os
from pathlib import Path
from typing import Optional
import aiohttp
from app.config import settings
from app.utils.anti_crawl import get_random_headers


def get_image_extension(url: str) -> str:
    url = url.lower()
    if url.endswith(('.jpg', '.jpeg')):
        return '.jpg'
    elif url.endswith('.png'):
        return '.png'
    elif url.endswith('.gif'):
        return '.gif'
    elif url.endswith('.webp'):
        return '.webp'
    return '.jpg'


def get_image_filename(url: str) -> str:
    hash_obj = hashlib.md5(url.encode('utf-8'))
    hash_str = hash_obj.hexdigest()
    ext = get_image_extension(url)
    return f"{hash_str}{ext}"


async def download_image(url: str, referer: str = None) -> Optional[str]:
    if not url:
        return None
    
    filename = get_image_filename(url)
    local_path = settings.IMAGES_DIR / filename
    
    if local_path.exists():
        return str(local_path.relative_to(settings.BASE_DIR))
    
    try:
        headers = get_random_headers(referer)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(local_path, 'wb') as f:
                        f.write(content)
                    return str(local_path.relative_to(settings.BASE_DIR))
    except Exception:
        pass
    
    return None
