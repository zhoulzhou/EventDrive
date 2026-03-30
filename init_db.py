#!/usr/bin/env python3
"""
数据库初始化脚本
用于创建数据库表和初始化默认筛选规则
"""
import asyncio
from app.config import settings
from app.database import engine, Base, SessionLocal
from app.models import News, FilterRule, CrawlLog
from app import crud


async def init_db():
    """初始化数据库"""
    print("正在创建数据库表...")
    
    Base.metadata.create_all(bind=engine)
    
    print("数据库表创建成功！")
    
    db = SessionLocal()
    try:
        existing_rule = crud.get_latest_filter_rule(db)
        if not existing_rule:
            print("创建默认筛选规则...")
            crud.create_filter_rule(
                db,
                include_keywords="",
                exclude_keywords=""
            )
            print("默认筛选规则创建成功！")
        else:
            print("筛选规则已存在，跳过创建")
    finally:
        db.close()
    
    print("\n数据库初始化完成！")


if __name__ == "__main__":
    asyncio.run(init_db())
