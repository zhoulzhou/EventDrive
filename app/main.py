import hashlib
import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.gzip import GZipMiddleware
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

# 指数预警:接口优先读 index/ CSV(按 mtime 缓存),CSV 可用时完全不碰数据库。
# index_history 表退化为降级数据源,仅在 CSV 缺失时才检查/导入。
# (原先靠 RELOAD_INDEX_DATA 环境变量触发重导,但发布工具注入不了环境变量,
#  线上 CSV 更新后无法生效 —— 改成读 CSV 后换文件即自动生效。)
if index_alarm.load_index_points_from_csv() is not None:
    print("✅ 指数预警数据源:index/ CSV(直接读取,按 mtime 缓存)")
else:
    print("⚠ 未读到 index/ CSV,降级使用 index_history 表")
    if settings.RELOAD_INDEX_DATA:
        index_alarm.reload_index_data()
    elif not index_alarm.ensure_index_data_loaded():
        print("⚠ 数据库中也无指数预警数据,该页面将为空")

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

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)

# 第三方库单独版本化：echarts.min.js 有 1MB，而自己的 CSS/JS 合计才 130KB。
# 若共用一个版本号，改一行样式也会让浏览器重下整个 echarts。
_VENDOR_FILES = ("js/echarts.min.js",)


def _content_version(files) -> str:
    """按文件**内容**算版本号。传入相对 static/ 的路径序列。

    这里原先用的是 ``int(time.time())``（进程启动瞬间的时间戳），后果是：
    沙箱实例每次重启（每次发布、每次被回收后冷启动）版本号都变 → 静态资源 URL 变
    → 浏览器长缓存在下一跳全部失效，1MB 的 echarts 又得重新下载一遍。
    用户感知就是「每次打开都要从网上拉」。

    改为内容哈希后，版本号只跟文件字节有关：部署多少次、重启多少个实例，
    只要文件没变，URL 就完全一致，浏览器一路命中 ``immutable`` 缓存（零请求）。
    只有真正改了那个文件，它的 URL 才变。

    启动时读一遍全部静态文件约 1.2MB，耗时十几毫秒，无感。
    """
    h = hashlib.sha1()
    for rel in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        path = static_dir / rel
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"missing")
        h.update(b"\x00")
    return h.hexdigest()[:10]


def _walk_static():
    """static/ 下所有文件的相对路径（posix 风格，排序保证哈希稳定）。"""
    return sorted(
        p.relative_to(static_dir).as_posix()
        for p in static_dir.rglob("*")
        if p.is_file()
    )


_ALL_STATIC = _walk_static()
# 自己的资源：排除第三方库，改了才有必要让浏览器重下
_OWN_STATIC = [rel for rel in _ALL_STATIC if rel not in _VENDOR_FILES]
jinja_env.globals["static_ver"] = _content_version(_OWN_STATIC) if _OWN_STATIC else 0
jinja_env.globals["vendor_ver"] = _content_version(_VENDOR_FILES)


class _CachedStaticFiles(StaticFiles):
    """给静态资源加**长缓存**，靠 URL 上的 ``?v={{ static_ver }}`` 做版本失效。

    此前这里是 ``no-store``（每次都回源），起因是改完样式刷新看不到变化。
    但代价太大：``echarts.min.js`` 有 1MB，每次打开任何图表页都要重新下载
    （实测冷启动 4.7 秒），而反代带宽有限，这是「网页慢」最直接的一环。

    一年 + immutable 后，浏览器在 URL 不变时**完全不发请求**。
    URL 的版本号由 `_content_version()` 按文件内容算出，与进程生命周期无关：
    重启、重新部署不会让它变，只有真正改了文件才变。
    本地调试改了 JS/CSS 而未重启服务时，用硬刷新（Ctrl+F5）绕过缓存。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


# 压缩中间件：**此前线上完全没有压缩**（实测 index-alarm 299KB、echarts 1MB 都是裸传）。
# 这些响应全是 JSON / 文本，gzip 后约为 1/6~1/10，对首屏是白捡的收益。
# 放在挂载 static 之前也覆盖静态文件（Starlette 的 middleware 包裹整个 ASGI 应用）。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

app.mount("/static", _CachedStaticFiles(directory=str(static_dir)), name="static")

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
