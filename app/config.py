import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    APP_NAME: str = os.getenv("APP_NAME", "EventDrive")
    APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
    DEBUG: bool = os.getenv("DEBUG", "True").lower() == "true"

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/db.sqlite3")

    CRAWL_INTERVAL_HOURS: int = int(os.getenv("CRAWL_INTERVAL_HOURS", "6"))
    NEWS_PER_SOURCE: int = int(os.getenv("NEWS_PER_SOURCE", "10"))
    NEWS_TIME_RANGE_HOURS: int = int(os.getenv("NEWS_TIME_RANGE_HOURS", "24"))

    MIN_DELAY: int = int(os.getenv("MIN_DELAY", "2"))
    MAX_DELAY: int = int(os.getenv("MAX_DELAY", "5"))

    IMAGES_DIR: Path = BASE_DIR / os.getenv("IMAGES_DIR", "./data/images")
    DATA_DIR: Path = BASE_DIR / "data"
    LOGS_DIR: Path = BASE_DIR / "logs"

    FEISHU_WEBHOOK_URL: str = os.getenv("FEISHU_WEBHOOK_URL", "")
    FEISHU_SECRET: str = os.getenv("FEISHU_SECRET", "")
    FEISHU_KEYWORD: str = os.getenv("FEISHU_KEYWORD", "头条")

    NYT_FEISHU_WEBHOOK_URL: str = os.getenv("NYT_FEISHU_WEBHOOK_URL", "")
    NYT_FEISHU_KEYWORD: str = os.getenv("NYT_FEISHU_KEYWORD", "HOT")

    BBC_FEISHU_WEBHOOK_URL: str = os.getenv("BBC_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/d7ce9b83-ea9e-4779-b514-211848d06e35")
    BBC_FEISHU_KEYWORD: str = os.getenv("BBC_FEISHU_KEYWORD", "HOT")

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.IMAGES_DIR.mkdir(exist_ok=True, parents=True)
        self.LOGS_DIR.mkdir(exist_ok=True)


settings = Settings()
