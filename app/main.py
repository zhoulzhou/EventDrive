import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.config import settings
from app.database import engine, Base
from app.api import news, crawl, filter, logs, feishu, login
from app.utils.feishu_notifier import init_feishu_notifier, init_nyt_feishu_notifier, init_bbc_feishu_notifier
from app.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)

print("=" * 60)
print("🚀 新闻抓取应用正在启动...")
print("=" * 60)

Base.metadata.create_all(bind=engine)
print("✅ 数据库表初始化完成")

if settings.FEISHU_WEBHOOK_URL and settings.FEISHU_SECRET:
    init_feishu_notifier(
        settings.FEISHU_WEBHOOK_URL,
        settings.FEISHU_SECRET,
        settings.FEISHU_KEYWORD
    )
    print("✅ 飞书推送已初始化")
else:
    print("⚠️ 飞书推送未配置 (FEISHU_WEBHOOK_URL 或 FEISHU_SECRET 未设置)")

if settings.NYT_FEISHU_WEBHOOK_URL:
    init_nyt_feishu_notifier(
        settings.NYT_FEISHU_WEBHOOK_URL,
        "",
        settings.NYT_FEISHU_KEYWORD
    )
    print("✅ 纽约时报飞书推送已初始化")
else:
    print("⚠️ 纽约时报飞书推送未配置 (NYT_FEISHU_WEBHOOK_URL 未设置)")

if settings.BBC_FEISHU_WEBHOOK_URL:
    init_bbc_feishu_notifier(
        settings.BBC_FEISHU_WEBHOOK_URL,
        "",
        settings.BBC_FEISHU_KEYWORD
    )
    print("✅ BBC飞书推送已初始化")
else:
    print("⚠️ BBC飞书推送未配置 (BBC_FEISHU_WEBHOOK_URL 未设置)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    print("✅ 定时任务调度器已启动")
    yield
    stop_scheduler()
    print("🛑 定时任务调度器已停止")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates_dir = BASE_DIR / "app" / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

data_images_dir = BASE_DIR / "data" / "images"
data_images_dir.mkdir(exist_ok=True, parents=True)
app.mount("/data/images", StaticFiles(directory=str(data_images_dir)), name="data_images")

app.include_router(news.router, prefix="/api", tags=["news"])
app.include_router(crawl.router, prefix="/api", tags=["crawl"])
app.include_router(filter.router, prefix="/api", tags=["filter"])
app.include_router(logs.router, prefix="/api", tags=["logs"])
app.include_router(feishu.router, prefix="/api", tags=["feishu"])
app.include_router(login.router, prefix="/api", tags=["login"])


def render_template(template_name: str, context: dict = None) -> HTMLResponse:
    template = jinja_env.get_template(template_name)
    context = context or {}
    html_content = template.render(**context)
    return HTMLResponse(content=html_content)


@app.get("/")
async def root(request: Request):
    return render_template("index.html", {"request": request})


@app.get("/login")
async def login_page(request: Request):
    return render_template("login.html", {"request": request})


@app.get("/news/{news_id}")
async def news_detail(request: Request, news_id: int):
    return render_template("news_detail.html", {"request": request, "news_id": news_id})


@app.get("/crawl")
async def crawl_control(request: Request):
    return render_template("crawl_control.html", {"request": request})


@app.get("/filter")
async def filter_rules(request: Request):
    return render_template("filter_rules.html", {"request": request})


@app.get("/logs")
async def crawl_logs(request: Request):
    return render_template("crawl_logs.html", {"request": request})
