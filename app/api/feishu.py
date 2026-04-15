import logging
from typing import List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.config import settings
from app.database import SessionLocal
from app import crud
from app.utils.feishu_notifier import get_feishu_notifier, notify_new_news
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()


class FeishuPushRequest(BaseModel):
    news_ids: List[int]


class FeishuPushResponse(BaseModel):
    success: bool
    message: str


@router.post("/feishu/push", response_model=FeishuPushResponse)
async def push_to_feishu(auth: bool = Depends(require_auth)):
    if not settings.FEISHU_WEBHOOK_URL:
        raise HTTPException(status_code=400, detail="飞书推送未配置")

    notifier = get_feishu_notifier()
    if not notifier:
        raise HTTPException(status_code=400, detail="飞书推送未初始化")

    db = SessionLocal()
    try:
        all_news = crud.get_news_list(db, skip=0, limit=100)

        if not all_news:
            return FeishuPushResponse(success=False, message="没有可推送的新闻")

        news_by_source = {}
        for news in all_news:
            source = news.source
            if source not in news_by_source:
                news_by_source[source] = []
            news_by_source[source].append(news)

        total_sent = 0
        results = []
        for source, news_list in news_by_source.items():
            news_data = [
                {
                    "title": news.title,
                    "url": news.url,
                    "publish_time": news.publish_time.isoformat() if news.publish_time else "",
                    "source": news.source
                }
                for news in news_list[:5]
            ]
            logger.info(f"飞书推送: {source} 准备推送 {len(news_data)} 条新闻")
            success = await notify_new_news(news_data, source)
            if success:
                total_sent += len(news_data)
                results.append(f"{source} {len(news_data)}条")

        if total_sent > 0:
            return FeishuPushResponse(success=True, message=f"成功推送: {', '.join(results)}")
        else:
            return FeishuPushResponse(success=False, message="推送失败，请查看日志")
    finally:
        db.close()
