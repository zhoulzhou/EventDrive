import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


class Settings:
    APP_NAME: str = os.getenv("APP_NAME", "EventDrive")
    APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
    DEBUG: bool = _get_bool("DEBUG", True)

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _get_int("PORT", 8000)

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/db.sqlite3")

    NEWS_PER_SOURCE: int = _get_int("NEWS_PER_SOURCE", 10)
    NEWS_TIME_RANGE_HOURS: int = _get_int("NEWS_TIME_RANGE_HOURS", 24)

    MIN_DELAY: int = _get_int("MIN_DELAY", 2)
    MAX_DELAY: int = _get_int("MAX_DELAY", 5)

    DATA_DIR: Path = BASE_DIR / "data"
    LOGS_DIR: Path = BASE_DIR / "logs"

    START_SCHEDULER: bool = _get_bool("START_SCHEDULER", False)

    # 市场指标自动抓取缓存有效期（小时）。超过则下次打开页面重新抓取
    MACRO_CACHE_TTL_HOURS: float = float(os.getenv("MACRO_CACHE_TTL_HOURS", "12"))

    # 数据库本地备份目录(纳入 git 管理, 备份文件按时间戳命名)
    DB_BACKUP_DIR: Path = BASE_DIR / "backup"

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.LOGS_DIR.mkdir(exist_ok=True)
        self.DB_BACKUP_DIR.mkdir(exist_ok=True)


settings = Settings()
