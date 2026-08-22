from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False, "timeout": 10} if "sqlite" in settings.DATABASE_URL else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    if "sqlite" in settings.DATABASE_URL:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA encoding='UTF-8'")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_compatibility(engine=engine):
    """检测并修复与当前模型不兼容的既有表结构。

    - market_prices: 旧版表结构为「每个 symbol 仅一条最新记录」并含 previous_value 列；
      新版改为「按 symbol + date 存储历史记录」，检测到旧结构直接删除重建
      （行情为日频数据，丢失后可由调度器重新抓取）。
    - financial_reports: 为既有表补充 stock_name 列（ALTER TABLE 增列，不删除数据）。
    """
    try:
        inspector = inspect(engine)
        if inspector.has_table("market_prices"):
            columns = {col["name"] for col in inspector.get_columns("market_prices")}
            if "previous_value" in columns:
                with engine.begin() as conn:
                    conn.execute(text("DROP TABLE market_prices"))
                print("[database] 检测到旧版 market_prices 表，已重建以支持历史数据")

        if inspector.has_table("financial_reports"):
            columns = {col["name"] for col in inspector.get_columns("financial_reports")}
            if "stock_name" not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE financial_reports ADD COLUMN stock_name TEXT"))
                print("[database] financial_reports 表已补充 stock_name 列")
    except Exception as e:
        print(f"[database] 表结构检测失败: {e}")
