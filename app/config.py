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

    FEISHU_WEBHOOK_URL: str = os.getenv("FEISHU_WEBHOOK_URL", "")
    FEISHU_SECRET: str = os.getenv("FEISHU_SECRET", "")
    FEISHU_KEYWORD: str = os.getenv("FEISHU_KEYWORD", "头条")

    NYT_API_KEY: str = os.getenv("NYT_API_KEY", "")
    NYT_FEISHU_WEBHOOK_URL: str = os.getenv("NYT_FEISHU_WEBHOOK_URL", "")
    NYT_FEISHU_KEYWORD: str = os.getenv("NYT_FEISHU_KEYWORD", "HOT")

    BBC_FEISHU_WEBHOOK_URL: str = os.getenv("BBC_FEISHU_WEBHOOK_URL", "")
    BBC_FEISHU_KEYWORD: str = os.getenv("BBC_FEISHU_KEYWORD", "HOT")

    DFCF_FEISHU_WEBHOOK_URL: str = os.getenv("DFCF_FEISHU_WEBHOOK_URL", "")
    DFCF_FEISHU_KEYWORD: str = os.getenv("DFCF_FEISHU_KEYWORD", "头条")

    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_FEISHU_WEBHOOK_URL: str = os.getenv("OPENROUTER_FEISHU_WEBHOOK_URL", "")
    OPENROUTER_KEYWORD: str = os.getenv("OPENROUTER_KEYWORD", "Talk")

    KB_API_KEY: str = os.getenv("KB_API_KEY", "")
    KB_MODEL_ID: str = os.getenv("KB_MODEL_ID", "doubao-1-5-pro-256k-250115")
    KB_REGION: str = os.getenv("KB_REGION", "cn-beijing")
    KB_FEISHU_WEBHOOK_URL: str = os.getenv("KB_FEISHU_WEBHOOK_URL", "")
    KB_KEYWORD: str = os.getenv("KB_KEYWORD", "Talk")

    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    DEEPSEEK_FEISHU_WEBHOOK_URL: str = os.getenv("DEEPSEEK_FEISHU_WEBHOOK_URL", "")
    DEEPSEEK_KEYWORD: str = os.getenv("DEEPSEEK_KEYWORD", "深度分析")

    X_B_T: str = os.getenv("X_B_T", "")
    X_FEISHU_WEBHOOK_URL: str = os.getenv("X_FEISHU_WEBHOOK_URL", "")
    X_FEISHU_KEYWORD: str = os.getenv("X_FEISHU_KEYWORD", "X推文")
    X_MAX_RESULTS: int = _get_int("X_MAX_RESULTS", 5)
    X_MONTH_MAX_LIMIT: int = _get_int("X_MONTH_MAX_LIMIT", 190)
    X_DAY_MAX_LIMIT: int = _get_int("X_DAY_MAX_LIMIT", 6)
    X_LIST_ID: str = os.getenv("X_LIST_ID", "")

    START_SCHEDULER: bool = _get_bool("START_SCHEDULER", False)

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.LOGS_DIR.mkdir(exist_ok=True)


settings = Settings()
