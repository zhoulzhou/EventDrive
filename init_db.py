#!/usr/bin/env python3
"""
数据库初始化脚本
用于创建数据库表
"""
import asyncio
from app.config import settings
from app.database import engine, Base, ensure_schema_compatibility
from app.models import News, CrawlLog, MarketPrice


async def init_db():
    """初始化数据库"""
    print("正在创建数据库表...")

    ensure_schema_compatibility(engine)
    Base.metadata.create_all(bind=engine)

    print("数据库表创建成功！")

    print("\n数据库初始化完成！")


if __name__ == "__main__":
    asyncio.run(init_db())
