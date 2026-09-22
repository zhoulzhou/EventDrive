import html
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
from app.api import news, crawl, backup, feishu, login, market, index_alarm, finance, valuation, macro, stock_temp
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

# 指数预警:CSV 更新后置 RELOAD_INDEX_DATA=1 强制重导;否则仅在表为空时导入一次,之后直接查库
if settings.RELOAD_INDEX_DATA:
    if index_alarm.reload_index_data():
        print("✅ 指数预警 CSV 数据已强制重新导入数据库")
    else:
        print("❌ 指数预警 CSV 强制重导失败")
elif index_alarm.ensure_index_data_loaded():
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
app.include_router(backup.router, prefix="/api", tags=["backup"])
app.include_router(feishu.router, prefix="/api", tags=["feishu"])
app.include_router(login.router, prefix="/api", tags=["login"])
app.include_router(market.router, prefix="/api", tags=["market"])
app.include_router(index_alarm.router, prefix="/api", tags=["index-alarm"])
app.include_router(finance.router, prefix="/api", tags=["finance"])
app.include_router(valuation.router, prefix="/api", tags=["valuation"])
app.include_router(macro.router, prefix="/api", tags=["macro"])
app.include_router(stock_temp.router, prefix="/api", tags=["stock-temp"])


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


@app.get("/finance")
async def finance_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("finance.html", {"request": request})


@app.get("/valuation")
async def valuation_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("valuation.html", {"request": request})


@app.get("/macro")
async def macro_page(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("macro.html", {"request": request})


@app.get("/macro-trend")
async def macro_trend_page(request: Request):
    """宏观趋势：只看历史曲线与走势（图表实现见 static/js/macro_charts.js）。"""
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("macro_trend.html", {"request": request})


@app.get("/stock-temp")
async def stock_temp_page(request: Request):
    """股票指标：只看当前读数与阈值分档（历史曲线见 /stock-trend）。"""
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("stock_temp.html", {"request": request})


@app.get("/stock-trend")
async def stock_trend_page(request: Request):
    """股票趋势：只看历史曲线与走势（图表实现见 static/js/stock_charts.js）。"""
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return render_template("stock_trend.html", {"request": request})


_PAGE_LINKS = [
    ("/home", "首页"),
    ("/crawl", "抓取"),
    ("/market", "行情"),
    ("/index-alarm", "指数预警"),
    ("/finance", "财务"),
    ("/valuation", "估值"),
    ("/macro", "宏观指标"),
    ("/macro-trend", "宏观趋势"),
    ("/stock-temp", "股票指标"),
    ("/stock-trend", "股票趋势"),
]


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """404 处理：/api、/static 保持 JSON 语义；页面路径返回带导航的友好 404 页。

    未匹配到路由时 FastAPI 默认只回 {"detail":"Not Found"} 裸 JSON，
    在浏览器里看到会很困惑（分不清是"路径写错"还是"服务挂了"）。
    """
    path = request.url.path
    if path.startswith("/api") or path.startswith("/static") or path.startswith("/docs") or path.startswith("/openapi"):
        return JSONResponse(status_code=404, content={"detail": "Not Found", "path": path})

    link_html = " · ".join(
        '<a href="%s">%s</a>' % (href, label) for href, label in _PAGE_LINKS
    )
    body = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>404 · 页面不存在</title>"
        '<link rel="stylesheet" href="/static/css/style.css"></head><body>'
        '<div class="container"><div class="error-state">'
        "<h1>404</h1>"
        "<p>没有这个页面：<code>%s</code></p>"
        '<p class="form-hint">可用页面：%s</p>'
        '<p><a class="btn" href="/home">返回首页</a></p>'
        "</div></div></body></html>"
    ) % (html.escape(path), link_html)
    return HTMLResponse(status_code=404, content=body)
