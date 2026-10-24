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


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
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

    # ---- 抓取限流（防止频繁抓取 / 单次抓取过多触发来源方风控）----
    # 同一 host 两次请求之间的最小间隔（秒），令牌桶按 host 独立计量
    RATE_LIMIT_MIN_INTERVAL: float = _get_float("RATE_LIMIT_MIN_INTERVAL", 3.0)
    # 令牌桶容量：允许的突发请求数
    RATE_LIMIT_BURST: int = _get_int("RATE_LIMIT_BURST", 3)
    # 每个 host 每天最多请求次数，0 表示不限制
    RATE_LIMIT_DAILY_QUOTA: int = _get_int("RATE_LIMIT_DAILY_QUOTA", 200)
    # 单次抓取任务内，单个 host 最多发起几次请求
    RATE_LIMIT_MAX_REQS_PER_RUN: int = _get_int("RATE_LIMIT_MAX_REQS_PER_RUN", 12)
    # 同一 host 两次「完整抓取任务」之间的最小间隔（秒），跨轮次冷却
    RATE_LIMIT_RUN_COOLDOWN: float = _get_float("RATE_LIMIT_RUN_COOLDOWN", 300.0)
    # 失败后的退避基数（秒）：连续失败按 base * 2^n 增长并封顶
    RATE_LIMIT_BACKOFF_BASE: float = _get_float("RATE_LIMIT_BACKOFF_BASE", 60.0)
    RATE_LIMIT_BACKOFF_MAX: float = _get_float("RATE_LIMIT_BACKOFF_MAX", 1800.0)
    # 触发限流后是否跳过抓取（true=直接放弃本轮，false=等待到冷却结束）
    RATE_LIMIT_SKIP_ON_EXCEED: bool = _get_bool("RATE_LIMIT_SKIP_ON_EXCEED", True)
    # 单个「任务级」每日最多执行次数（用于 akshare 等内部封装、无法逐请求插桩的来源）
    TASK_DAILY_QUOTA: int = _get_int("TASK_DAILY_QUOTA", 8)

    DATA_DIR: Path = BASE_DIR / "data"
    LOGS_DIR: Path = BASE_DIR / "logs"

    # 注：原 START_SCHEDULER 配置项已移除。进程内 APScheduler 已停用
    #（云沙箱回收进程 → job 只在进程启动时算出 next_run_time，不会补跑），
    # 定时抓取改由外部自动任务调用 POST /api/crawl/daily-refresh 点火。
    # CSV 更新后置 1 可强制重导 index_history 表
    RELOAD_INDEX_DATA: bool = _get_bool("RELOAD_INDEX_DATA", False)

    # ---- 云数据库（WorkBuddy Cloud / PostgREST）----
    #
    # 沙箱部署无持久磁盘，SQLite 重启即丢。启用后快照类小表改走云库持久化。
    # 三项须齐备才启用，缺任一项自动退回 SQLite（见 app/cloud_db.py）。
    #
    # 这里的默认值是**该应用自己的 publicConfig**：publishableKey 是前端可见的
    # 应用标识（服务端只校验 Origin，不含管理权限），可安全随源码分发；
    # 真正的环境 id 与 provider 密钥始终留在服务端，不出现在这里。
    # 若将来多应用共用一套代码，改用环境变量覆盖即可。
    # 缺省为 false：本地默认继续用 SQLite（离线可调试）。
    # 云沙箱不走这个开关 —— app/cloud_db.py 检测到 PORT 注入会直接启用云库，
    # 这样即使 .env（含 CLOUD_DB_ENABLED=false）被一起打包也不会把线上打回 SQLite。
    CLOUD_DB_ENABLED: bool = _get_bool("CLOUD_DB_ENABLED", False)
    CLOUD_DB_ENDPOINT: str = os.getenv(
        "CLOUD_DB_ENDPOINT",
        "https://eventdrive-finance.app.workbuddy.host")
    CLOUD_DB_KEY: str = os.getenv(
        "CLOUD_DB_KEY",
        "wbpk_qSd54InvBTGATZPZuNrkZA_Nuy2RbjRqRfNnWOMEf5fJP1z5DqFzpsW")

    # 云库**读缓存**有效期（秒）。0 = 关闭。
    #
    # 为什么需要：云库每次往返 0.25~0.9 秒（网关 + 库侧开销，与浏览器链路无关），
    # 而本应用的数据是「每天批量更新一次」——9:15 抓新闻、16:17 收盘轮刷新指标。
    # 同一份数据被反复读，却每次都付一次完整往返。
    # 加读缓存后：命中时零往返，页面从 1~3 秒降到 0.2~0.4 秒（只剩反代开销）。
    #
    # 一致性：**任何写操作成功后整体失效**（见 cloud_db._invalidate_cache），
    # 所以「抓取完立刻刷新页面」不会读到旧值；120 秒只覆盖纯读场景。
    CLOUD_DB_CACHE_TTL: float = float(os.getenv("CLOUD_DB_CACHE_TTL", "120"))

    # 市场指标自动抓取缓存有效期（小时）。超过则下次打开页面重新抓取
    MACRO_CACHE_TTL_HOURS: float = float(os.getenv("MACRO_CACHE_TTL_HOURS", "12"))

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.LOGS_DIR.mkdir(exist_ok=True)


settings = Settings()
