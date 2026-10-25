import os
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings


def _resolve_database_url() -> str:
    """确定实际使用的数据库地址，并在默认路径不可写时降级。

    云沙箱部署时，项目目录可能只读（或自带一个不可写的 db.sqlite3），
    此时 SQLite 连「打开 + 设WAL」都会失败，导致进程直接崩溃、端口起不来。
    这里做一次可写性探测：默认库不可写时，改用系统临时目录下的新库，
    保证服务至少能起来（数据从空库开始，靠抓取任务逐步积累）。
    """
    url = settings.DATABASE_URL
    if "sqlite" not in url:
        return url

    # 从 sqlite:///path 形式解析出文件路径
    raw = url.split("///", 1)[-1]
    path = Path(raw) if raw and raw != ":memory:" else None

    def _writable(p: Path) -> bool:
        try:
            # 已存在：直接探测能否写入
            if p.exists():
                return os.access(p, os.W_OK)
            # 不存在：探测父目录能否创建
            parent = p.parent
            parent.mkdir(parents=True, exist_ok=True)
            return os.access(parent, os.W_OK)
        except OSError:
            return False

    if path is None or _writable(path):
        return url

    # 降级：临时目录
    fallback = Path(tempfile.gettempdir()) / "eventdrive" / "db.sqlite3"
    try:
        fallback.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "eventdrive_fallback.sqlite3"

    import logging
    logging.getLogger(__name__).warning(
        "数据库 %s 不可写，已降级到临时目录: %s", path, fallback
    )
    return f"sqlite:///{fallback}"


DATABASE_URL = _resolve_database_url()

connect_args = {"check_same_thread": False, "timeout": 10} if "sqlite" in DATABASE_URL else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    if "sqlite" in DATABASE_URL:
        cursor = dbapi_conn.cursor()
        # WAL 模式需要写权限；若所在文件系统不支持（如部分只读挂载），
        # 降级为默认 journal 模式，避免整条连接失败。
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
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

        if inspector.has_table("company_valuations"):
            columns = {col["name"] for col in inspector.get_columns("company_valuations")}
            if "current_market_value" not in columns:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE company_valuations ADD COLUMN current_market_value FLOAT"))
                print("[database] company_valuations 表已补充 current_market_value 列")
            # transition_years / growth_transition 已于 2026-10-25 下线（不再写入/展示），
            # 故不再为老库补这两列；已有列保留不动，历史行仍可查。
    except Exception as e:
        print(f"[database] 表结构检测失败: {e}")