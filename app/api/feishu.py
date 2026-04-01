import logging
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.database import SessionLocal
from app import crud
from app.utils.feishu_notifier import get_feishu_notifier, notify_new_news

logger = logging.getLogger(__name__)

router = APIRouter()


class FeishuPushRequest(BaseModel):
    news_ids: List[int]


class FeishuPushResponse(BaseModel):
    success: bool
    message: str


@router.post("/feishu/push", response_model=FeishuPushResponse)
async def push_to_feishu():
    if not settings.FEISHU_WEBHOOK_URL:
        raise HTTPException(status_code=400, detail="飞书推送未配置")

    notifier = get_feishu_notifier()
    if not notifier:
        raise HTTPException(status_code=400, detail="飞书推送未初始化")

    db = SessionLocal()
    try:
        news_list = crud.get_news_list(db, skip=0, limit=10)

        if not news_list:
            return FeishuPushResponse(success=False, message="没有可推送的新闻")

        news_data = [
            {
                "title": news.title,
                "url": news.url,
                "publish_time": news.publish_time.isoformat() if news.publish_time else "",
                "source": news.source
            }
            for news in news_list
        ]

        logger.info(f"飞书推送: 准备推送 {len(news_data)} 条新闻")
        success = await notify_new_news(news_data, "新闻汇总")
        logger.info(f"飞书推送: notify_new_news 返回结果: {success}")

        if success:
            return FeishuPushResponse(success=True, message=f"成功推送 {len(news_data)} 条新闻到飞书")
        else:
            return FeishuPushResponse(success=False, message="推送失败，请查看日志")
    finally:
        db.close()
