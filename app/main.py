import html
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.config import settings
from app.database import engine, Base, ensure_schema_compatibility
from app.api import news, crawl, login, market, index_alarm, finance, valuation, macro, stock_temp
from app.api.login import is_logged_in

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

print("=" * 60)
print("🚀 市场总览应用正在启动...")
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 定时抓取由**外部自动任务**调用 POST /api/crawl/daily-refresh 点火
    # （北京时间 16:00 收盘一轮，不含财务指标）。
    # 进程内 APScheduler 已停用 —— 云沙箱会回收进程，job 只在进程启动那一刻
    # 算出 next_run_time、无持久化 job store，定时点过后不会补跑。
    print("ℹ️  定时抓取由外部自动任务点火（POST /api/crawl/daily-refresh），进程内调度器已停用")
    yield
    print("🛑 服务关闭")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates_dir = BASE_DIR / "app" / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))
# 静态资源缓存击穿的时间戳：进程启动瞬间生成，所有页面通过 {{ static_ver }} 取用，
# 服务重启一次 = 全部 CSS/JS 的 URL 变化，浏览器必然重新拉取。
jinja_env.globals["static_ver"] = int(time.time())

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)


class _NoCacheStaticFiles(StaticFiles):
    """给静态资源加上 no-store，避免浏览器长期缓存旧的 CSS/JS。

    默认的 StaticFiles 只发 ETag、不发 Cache-Control，浏览器在「离线可用/刷新」等
    策略下可能继续用旧副本，改完样式却看不到变化（这轮宏观温度计布局调整就踩到过）。
    这里明确要求每次回源校验，改完刷新即生效；文件都是本地磁盘读，开销可忽略。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


app.mount("/static", _NoCacheStaticFiles(directory=str(static_dir)), name="static")

app.include_router(news.router, prefix="/api", tags=["news"])
app.include_router(crawl.router, prefix="/api", tags=["crawl"])
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
        # 定时抓取由外部自动任务点火，不在本进程内调度
        "scheduler": "external",
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
    ("/stock-temp", "股市热度"),
    ("/stock-trend", "热度趋势"),
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
