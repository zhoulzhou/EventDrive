import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.config import settings
from app.database import engine, Base, ensure_schema_compatibility
from app.api import news, crawl, filter, logs, feishu, login, market, index_alarm
from app.utils.feishu_notifier import init_all_notifiers, start_notifier, shutdown_notifier
from app.scheduler import start_scheduler, stop_scheduler, scheduler as sched_instance
from app.api.login import is_logged_in

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

print("=" * 60)
print("🚀 新闻抓取应用正在启动...")
print("=" * 60)

ensure_schema_compatibility(engine)
Base.metadata.create_all(bind=engine)
print("✅ 数据库表初始化完成")

# 指数预警:仅首次(表为空)从 index/ CSV 导入一次,之后全部直接查库
if index_alarm.ensure_index_data_loaded():
    print("✅ 指数预警 CSV 数据已一次性导入数据库")
else:
    print("✅ 指数预警数据已存在,直接使用数据库缓存")

init_all_notifiers(
    nyt_url=settings.NYT_FEISHU_WEBHOOK_URL or "",
    nyt_keyword=settings.NYT_FEISHU_KEYWORD,
    bbc_url=settings.BBC_FEISHU_WEBHOOK_URL or "",
    bbc_keyword=settings.BBC_FEISHU_KEYWORD,
    dfcf_url=settings.DFCF_FEISHU_WEBHOOK_URL or "",
    dfcf_keyword=settings.DFCF_FEISHU_KEYWORD,
    kb_url=settings.KB_FEISHU_WEBHOOK_URL or "",
    kb_keyword=settings.KB_KEYWORD,
    openrouter_url=settings.OPENROUTER_FEISHU_WEBHOOK_URL or "",
    openrouter_keyword=settings.OPENROUTER_KEYWORD,
    deepseek_url=settings.DEEPSEEK_FEISHU_WEBHOOK_URL or "",
    deepseek_keyword=settings.DEEPSEEK_KEYWORD,
    x_url=settings.X_FEISHU_WEBHOOK_URL or "",
    x_keyword=settings.X_FEISHU_KEYWORD,
)
print("✅ 飞书推送初始化完成")
if settings.DEEPSEEK_FEISHU_WEBHOOK_URL:
    print(f"  - DeepSeek 飞书推送: ✅ (关键词: {settings.DEEPSEEK_KEYWORD})")
if settings.KB_FEISHU_WEBHOOK_URL:
    print(f"  - 豆包飞书推送: ✅ (关键词: {settings.KB_KEYWORD})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_notifier()
    if settings.START_SCHEDULER:
        start_scheduler()
        jobs = sched_instance.get_jobs()
        print("✅ 定时任务调度器已启动（内嵌模式）")
        for job in jobs:
            print(f"   - {job.name} (next: {job.next_run_time})")
    else:
        print("ℹ️  Web 进程仅提供 API 服务，定时任务由独立进程 run_scheduler.py 运行")
        print("   启动方式: python run_scheduler.py  或  ./start.sh")
    yield
    if settings.START_SCHEDULER:
        stop_scheduler()
        print("🛑 定时任务调度器已停止")
    await shutdown_notifier()


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

app.include_router(news.router, prefix="/api", tags=["news"])
app.include_router(crawl.router, prefix="/api", tags=["crawl"])
app.include_router(filter.router, prefix="/api", tags=["filter"])
app.include_router(logs.router, prefix="/api", tags=["logs"])
app.include_router(feishu.router, prefix="/api", tags=["feishu"])
app.include_router(login.router, prefix="/api", tags=["login"])
app.include_router(market.router, prefix="/api", tags=["market"])
app.include_router(index_alarm.router, prefix="/api", tags=["index-alarm"])


def render_template(template_name: str, context: dict = None) -> HTMLResponse:
    template = jinja_env.get_template(template_name)
    context = context or {}
    html_content = template.render(**context)
    return HTMLResponse(content=html_content)


@app.get("/health")
async def health_check():
    return JSONResponse({
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "scheduler_running": sched_instance.running,
    })


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


@app.get("/market")
async def market_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("market.html", {"request": request})


@app.get("/index-alarm")
async def index_alarm_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("index_alarm.html", {"request": request})
