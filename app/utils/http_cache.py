"""日更 JSON 接口的协商缓存（ETag / 304）。

为什么不用 max-age
------------------
页面「刷新数据」按钮会在后台抓取完成后重新拉**同一个 URL**。若给浏览器本地
max-age，用户在窗口内会继续看到旧读数，还得解释「等一会儿再刷新」。因此用
`private, no-cache, must-revalidate`：浏览器每次都发条件请求，数据没变时服务端
直接 304，跳过整页重算（builder 的 5~10 次查询）；数据变了才返回 200 全新响应。

- `private`：登录态数据，禁止共享代理缓存。
- ETag 由 view_key + schema 版本 + 数据指纹/快照构建时间组成，口径变更
  （SCHEMA_VERSION +1）或抓取落库后必然变化，不会把旧页面钉死。
- 任何异常由 view_cache.peek_stamp() 吞掉，ETag 退化成空指纹，最坏情况是
  每次都返回 200，不影响正确性。

浏览器 fetch 对条件请求会透明地把 304 还原成缓存的 200 响应，所以前端
fetchWithAuth 的 `response.ok` 判断不用改。
"""
import hashlib

from fastapi import Request, Response

from app.utils import view_cache

CACHE_CONTROL = "private, no-cache, must-revalidate"


def etag(view_key: str, extra: str = "") -> str:
    raw = "%s|v%s|%s|%s" % (view_key, view_cache.SCHEMA_VERSION, extra,
                             view_cache.peek_stamp(view_key))
    return '"' + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16] + '"'


def not_modified(request: Request, tag: str):
    """命中 If-None-Match 时返回可直接 return 的 304 响应，否则 None。"""
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={
            "ETag": tag,
            "Cache-Control": CACHE_CONTROL,
        })
    return None


def apply(response: Response, tag: str) -> None:
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = CACHE_CONTROL
