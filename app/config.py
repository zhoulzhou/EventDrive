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

    NYT_API_KEY: str = os.getenv("NYT_API_KEY", "")
    NYT_FEISHU_WEBHOOK_URL: str = os.getenv("NYT_FEISHU_WEBHOOK_URL", "")
    NYT_FEISHU_KEYWORD: str = os.getenv("NYT_FEISHU_KEYWORD", "HOT")

    BBC_FEISHU_WEBHOOK_URL: str = os.getenv("BBC_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/3d2a80af-aa97-48aa-864e-dec19d48ac08")
    BBC_FEISHU_KEYWORD: str = os.getenv("BBC_FEISHU_KEYWORD", "HOT")

    EM_FEISHU_WEBHOOK_URL: str = os.getenv("EM_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/1d418960-b397-428e-9808-f9a36a0e2edf")
    EM_FEISHU_KEYWORD: str = os.getenv("EM_FEISHU_KEYWORD", "头条")

    KB_APIKEY: str = os.getenv("KB_APIKEY", "8ZZGZ1SM19RP0X1991HXDFH674FCPD6H580TZABCZ9SCP5TP6G8060R30C9K60SKA")
    KB_SERVICE_ID: str = os.getenv("KB_SERVICE_ID", "kb-service-97251f0167acebc4")
    KB_REGION: str = os.getenv("KB_REGION", "cn-beijing")
    KB_FEISHU_WEBHOOK_URL: str = os.getenv("KB_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/dea274be-df47-4ab7-b69b-ec51de9c3e17")
    KB_KEYWORD: str = os.getenv("KB_KEYWORD", "Talk")

    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "d7f34c1r01qi33g87p90d7f34c1r01qi33g87p9g")
    INDEX_FEISHU_WEBHOOK_URL: str = os.getenv("INDEX_FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/5977e5fe-e551-4091-829d-2d1e89edd721")
    INDEX_KEYWORD: str = os.getenv("INDEX_KEYWORD", "指数")

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.IMAGES_DIR.mkdir(exist_ok=True, parents=True)
        self.LOGS_DIR.mkdir(exist_ok=True)


settings = Settings()
