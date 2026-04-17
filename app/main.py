import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.config import settings
from app.database import engine, Base
from app.api import news, crawl, filter, logs, feishu, login
from app.utils.feishu_notifier import init_feishu_notifier, init_all_notifiers
from app.scheduler import start_scheduler, stop_scheduler
from app.api.login import is_logged_in

logger = logging.getLogger(__name__)

print("=" * 60)
print("🚀 新闻抓取应用正在启动...")
print("=" * 60)

Base.metadata.create_all(bind=engine)
print("✅ 数据库表初始化完成")

init_all_notifiers(
    feishu_url=settings.FEISHU_WEBHOOK_URL,
    feishu_secret=settings.FEISHU_SECRET or "",
    feishu_keyword=settings.FEISHU_KEYWORD,
    nyt_url=settings.NYT_FEISHU_WEBHOOK_URL or "",
    nyt_keyword=settings.NYT_FEISHU_KEYWORD,
    bbc_url=settings.BBC_FEISHU_WEBHOOK_URL or "",
    bbc_keyword=settings.BBC_FEISHU_KEYWORD,
    dfcf_url=settings.DFCF_FEISHU_WEBHOOK_URL or "",
    dfcf_keyword=settings.DFCF_FEISHU_KEYWORD,
    cls_url=settings.CLS_FEISHU_WEBHOOK_URL or "",
    cls_keyword=settings.CLS_FEISHU_KEYWORD,
    index_url=settings.INDEX_FEISHU_WEBHOOK_URL or "",
    index_keyword=settings.INDEX_KEYWORD,
    kb_url=settings.KB_FEISHU_WEBHOOK_URL or "",
    kb_keyword=settings.KB_KEYWORD,
    openrouter_url=settings.OPENROUTER_FEISHU_WEBHOOK_URL or "",
    openrouter_keyword=settings.OPENROUTER_KEYWORD,
)
print("✅ 飞书推送初始化完成")


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
    return RedirectResponse(url="/login")


@app.get("/home")
async def home(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("index.html", {"request": request})


@app.get("/login")
async def login_page(request: Request):
    return render_template("login.html", {"request": request})


@app.get("/news/{news_id}")
async def news_detail(request: Request, news_id: int):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("news_detail.html", {"request": request, "news_id": news_id})


@app.get("/crawl")
async def crawl_control(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("crawl_control.html", {"request": request})


@app.get("/filter")
async def filter_rules(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("filter_rules.html", {"request": request})


@app.get("/logs")
async def crawl_logs(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("crawl_logs.html", {"request": request})
