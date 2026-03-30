import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app.config import settings
from app.database import engine, Base
from app.api import news, crawl, filter, logs

logger = logging.getLogger(__name__)

print("=" * 60)
print("🚀 新闻抓取应用正在启动...")
print("=" * 60)

Base.metadata.create_all(bind=engine)
print("✅ 数据库表初始化完成")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates_dir = BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

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


@app.get("/")
async def root():
    return {"message": "新闻抓取应用正在运行", "version": "1.0.0", "status": "ok"}


@app.get("/news/{news_id}")
async def news_detail(request: Request, news_id: int):
    return templates.TemplateResponse("news_detail.html", {"request": request, "news_id": news_id})


@app.get("/crawl")
async def crawl_control(request: Request):
    return templates.TemplateResponse("crawl_control.html", {"request": request})


@app.get("/filter")
async def filter_rules(request: Request):
    return templates.TemplateResponse("filter_rules.html", {"request": request})


@app.get("/logs")
async def crawl_logs(request: Request):
    return templates.TemplateResponse("crawl_logs.html", {"request": request})
