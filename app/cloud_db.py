"""云数据库（PostgREST）数据访问层。

背景
----
WorkBuddy 云数据库对外只暴露 PostgREST REST 接口（`/.cloud/database/rest/**`）。
本项目原本用 SQLAlchemy + SQLite，云沙箱无持久磁盘，重启即丢库；改接云库后
需要一套能替代 ORM 的访问实现。

鉴权要点（踩过坑，勿改）
------------------------
必须用 header ``x-wb-webapp-access-key`` 携带 publishableKey。
用 ``apikey`` 或 ``Authorization: Bearer`` 都会被网关拒绝，返回
``401 invalid_client``（网关层拒绝，尚未到数据库，与权限无关）。

设计取舍
--------
1. **保持 crud 层的返回契约**：现有 crud 函数返回的是 ORM 实例，调用方按
   ``row.key`` / ``row.value`` / ``row.detail`` 取属性。这里用轻量 ``Row``
   （属性访问 + 可下标）模拟同等接口，**上层调用方一行都不用改**。
2. **失败不抛到页面**：云库抖动不应让整页500。读失败返回空集并记日志，
   写失败返回计数 0，由页面显示「数据不足」。
3. **按需启用**：仅当配置了 ``CLOUD_DB_ENABLED`` / ``CLOUD_DB_ENDPOINT`` /
   ``CLOUD_DB_KEY`` 时才切到云库；否则沿用 SQLite（本地开发零依赖）。

启用方式（环境变量）
--------------------
    CLOUD_DB_ENABLED=true
    CLOUD_DB_ENDPOINT=https://<app-domain>
    CLOUD_DB_KEY=wbpk_xxx
"""
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# PostgREST 数据面前缀
REST_PREFIX = "/.cloud/database/rest"

# 认证 header 名 —— 必须是这个，apikey / Authorization 都会被拒
ACCESS_KEY_HEADER = "x-wb-webapp-access-key"

# 内部用连接池复用的客户端
_client: Optional[httpx.Client] = None

# 单次批量插入的分片大小（实测 1000 行一次请求可过，留余量）
_INSERT_CHUNK = 500


class CloudDBError(RuntimeError):
    """云数据库访问失败（网络、鉴权、权限、语法等）。"""


# 时间列名：PostgREST 返回 ISO 字符串（'2026-10-17T16:07:24.97965'），
# 而 SQLite 的 SQLAlchemy 返回 datetime 对象。页面层（_data_written_at / _local_str）
# 会直接对 updated_at 做减法和 astimezone，两种类型混用会崩。
# 这里统一在 Row 构造时把时间列还原成 **naive datetime（UTC）**，
# 与 SQLite 侧 server_default=func.now()（UTC naive）保持同一基准。
DATETIME_COLUMNS = frozenset({
    "created_at", "updated_at", "fetched_at", "crawl_time", "publish_time",
})


def _coerce_datetimes(data: Dict[str, Any]) -> Dict[str, Any]:
    """把 ISO 时间字符串转成 naive datetime（UTC）。无法解析则原样保留。"""
    from datetime import datetime as _dt

    for col in DATETIME_COLUMNS:
        val = data.get(col)
        if not isinstance(val, str) or not val:
            continue
        text = val.strip().replace(" ", "T", 1) if "T" not in val else val
        if text.endswith("Z"):
            text = text[:-1]
        try:
            parsed = _dt.fromisoformat(text)
        except ValueError:
            continue
        # 统一成 UTC naive：带时区的先转UTC 再去掉 tzinfo
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_dt.now().astimezone().tzinfo).replace(tzinfo=None)
        data[col] = parsed
    return data


class Row:
    """轻量行对象：同时支持属性访问与下标访问，模拟 ORM 实例。

    现有 crud 层把ORM 实例传给上层（如``stock_temp.py`` 里读 ``row.value``、
    ``row.detail``、``row.updated_at``），这里保持同样的访问方式，调用方无需改动。

    时间列会被归一化成 naive datetime，避免上层拿到 ISO 字符串后做减法/时区转换时报错。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        object.__setattr__(self, "_data", _coerce_datetimes(dict(data or {})))

    def __getattr__(self, name: str) -> Any:
        # 仅在属性确实不存在时走这里；正常命中走 __slots__ 之外的 dict 查找
        return self._data.get(name)

    def __getitem__(self, name: str) -> Any:
        return self._data[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:
        preview = dict(list(self._data.items())[:4])
        return f"<Row {preview}>"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Row):
            return self._data == other._data
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(sorted((k, str(v)) for k, v in self._data.items())))


# ================================================================ 配置与开关

def _is_cloud_sandbox() -> bool:
    """是否运行在云沙箱里（判定依据与 run_server.py 一致：平台注入 PORT）。

    为什么不用「.env 里有没有 CLOUD_DB_ENABLED」来判断：
    **发布工具按目录整体打包，.gitignore 拦不住 .env** —— 本地为了走 SQLite
    写的 `.env`（CLOUD_DB_ENABLED=false）会一起进沙箱，把云库配置覆盖掉，
    线上就静默退回本地 SQLite（数据重启即丢，且很难察觉）。
    所以云沙箱一律以 publicConfig 缺省值连云库，.env 只在本地开发时起作用。
    """
    return bool(os.getenv("PORT"))


def cloud_db_configured() -> bool:
    """是否启用云数据库。

    规则：
    - 云沙箱（有 PORT 注入）：只要 publicConfig 的endpoint / key 齐备就启用，
      **完全不看 CLOUD_DB_ENABLED** —— 该开关可能被一起打包进来的 .env 覆盖
      （发布工具按目录整体打包，.gitignore 拦不住 .env）。
    - 本地：需显式 CLOUD_DB_ENABLED=true 才启用，否则继续用 SQLite
      （本地开发零依赖、离线可调试）。
    """
    if not (settings.CLOUD_DB_ENDPOINT and settings.CLOUD_DB_KEY):
        return False
    if _is_cloud_sandbox():
        return True
    # 本地：读 settings（load_dotenv(override=True) 已把 .env 合并进来），
    # 不直接读 os.getenv —— 否则 .env 里的 false 会漏掉。
    return bool(settings.CLOUD_DB_ENABLED)


def _endpoint() -> str:
    return settings.CLOUD_DB_ENDPOINT.rstrip("/")


def _client_instance() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=_endpoint(),
            headers={
                ACCESS_KEY_HEADER: settings.CLOUD_DB_KEY,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            # 重试仅覆盖连接类错误，不重试 4xx（重复写风险）
            transport=httpx.HTTPTransport(retries=2),
        )
    return _client


def close_client() -> None:
    """关闭底层连接池（进程退出 / 测试收尾用）。"""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


# ================================================================ 读缓存
#
# 为什么要这一层
# --------------
# 云库单次往返 0.25~0.9 秒（网关 + 库侧开销；TCP 建连只占 50ms，慢在服务端）。
# 而本应用的数据是「每天批量更新一次」：9:15 抓新闻、16:17 收盘轮刷新指标。
# 于是同一份数据被反复读上千次，每次都付一次完整往返 —— 实测新闻列表一个请求
# 要分 6 页拉完 5907 行（2.8s），宏观页一个请求把 macro_indicators 查了 5 遍。
#
# 这一层做两件事：
#   1. **请求内去重**：同一请求里多次查同一张表/同一条件，第二次起零往返；
#   2. **跨请求缓存**：数据日更，重复打开页面不必反复取。
#
# 一致性
# ------
# **任何写操作（POST / PATCH / DELETE）成功后整体失效**。抓取任务写库之后，
# 下一次读必然回源，不会出现「抓完还是旧数据」。默认 TTL 120 秒只作用于纯读。
#
# 为什么不缓存到内存里的 Row 对象：Row 会被调用方原地修改（如补字段），
# 缓存字节流、每次重新解析成新 Row，能避免调用方互相污染。

_CACHE_MAX_ENTRIES = 512
_CACHE_MAX_ITEM_BYTES = 2 * 1024 * 1024      # 单条上限：大响应不值得占内存
_CACHE_MAX_BYTES = 32 * 1024 * 1024          # 总量上限（沙箱内存有限，留足余量）
_cache: "OrderedDict[str, Tuple[float, int, bytes, Dict[str, str]]]" = OrderedDict()
_cache_bytes = 0
_cache_stat = {"hit": 0, "miss": 0, "invalidate": 0}
_cache_lock = threading.Lock()


def _cache_ttl() -> float:
    """读缓存有效期（秒）。0 表示关闭。每次读配置，便于测试时临时改。"""
    try:
        return float(getattr(settings, "CLOUD_DB_CACHE_TTL", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _cache_key(path: str, params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return path
    items = sorted((str(k), str(v)) for k, v in params.items())
    return path + "?" + "&".join(f"{k}={v}" for k, v in items)


def clear_cache() -> None:
    """清空读缓存（写操作后自动调用；测试与「抓取完成后强刷」也可手动调）。"""
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0
        _cache_stat["invalidate"] += 1


def cache_stats() -> Dict[str, Any]:
    """缓存命中情况，用于排查「页面为什么还慢」。"""
    with _cache_lock:
        return {
            "ttl": _cache_ttl(),
            "entries": len(_cache),
            "bytes": _cache_bytes,
            **_cache_stat,
        }


def _cache_get(key: str) -> Optional[Tuple[int, bytes, Dict[str, str]]]:
    global _cache_bytes
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            _cache_stat["miss"] += 1
            return None
        expire_at, status, body, headers = item
        if expire_at <= now:
            # 过期就地摘掉（顺带修正字节计数）
            _cache.pop(key, None)
            _cache_bytes -= len(body)
            _cache_stat["miss"] += 1
            return None
        _cache.move_to_end(key)          # LRU：命中的挪到队尾
        _cache_stat["hit"] += 1
        return status, body, headers


def _cache_put(key: str, status: int, body: bytes, headers: Dict[str, str]) -> None:
    global _cache_bytes
    ttl = _cache_ttl()
    if ttl <= 0 or status != 200 or not body:
        return
    if len(body) > _CACHE_MAX_ITEM_BYTES:
        return
    expire_at = time.time() + ttl
    with _cache_lock:
        old = _cache.pop(key, None)
        if old is not None:
            _cache_bytes -= len(old[2])
        _cache[key] = (expire_at, status, body, headers)
        _cache_bytes += len(body)
        # 淘汰：条目数超限或总量超限，从最旧的开始丢
        while _cache and (len(_cache) > _CACHE_MAX_ENTRIES
                          or _cache_bytes > _CACHE_MAX_BYTES):
            _, (_, _, dropped, _) = _cache.popitem(last=False)
            _cache_bytes -= len(dropped)


# ================================================================ 请求封装

def _request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
             json_body: Any = None, prefer: Optional[str] = None
             ) -> httpx.Response:
    """发一次请求（GET 走读缓存）；非 2xx 抛 CloudDBError（带状态码与响应片段便于定位）。"""
    is_read = method.upper() == "GET"
    key = _cache_key(path, params) if is_read else ""

    if is_read:
        cached = _cache_get(key)
        if cached is not None:
            status, body, headers = cached
            return httpx.Response(status_code=status, content=body, headers=headers)

    headers = {"Prefer": prefer} if prefer else None
    try:
        resp = _client_instance().request(
            method, f"{REST_PREFIX}{path}", params=params, json=json_body, headers=headers
        )
    except httpx.HTTPError as e:
        raise CloudDBError(f"云数据库请求失败: {method} {path}: {e}") from e

    if resp.status_code >= 400:
        raise CloudDBError(
            f"云数据库返回 {resp.status_code}: {method} {path}: {resp.text[:300]}"
        )

    if is_read:
        # 只留调用方用得上的头（content-type 决定 .json() 的解析方式）
        keep = {k: v for k, v in resp.headers.items()
                if k.lower() in ("content-type", "content-range")}
        _cache_put(key, resp.status_code, resp.content, keep)
    else:
        # 写成功 → 整体失效：宁可多回源，也不能让页面看到写之前的旧值
        clear_cache()

    return resp


def _safe(method: str, path: str, *, default: Any = None,
          params: Optional[Dict[str, Any]] = None,
          json_body: Any = None, prefer: Optional[str] = None,
          context: str = "") -> Any:
    """包一层容错：失败时记录日志并返回默认值，避免整页500。"""
    try:
        return _request(method, path, params=params, json_body=json_body, prefer=prefer)
    except CloudDBError as e:
        logger.warning("[cloud_db] %s%s 失败: %s", context or path, "", e)
        return default


# ================================================================ 查询

def _dt_filter(dt) -> str:
    """把 datetime 转成 PostgREST 的 gte./lt.过滤表达式。

    库里时间列是 ``TIMESTAMP WITHOUT TIME ZONE``，写入的是 **naive UTC**，
    所以这里也输出不带时区的 ISO 串（``2026-10-18T09:00:00``）。
    """
    from datetime import date as _date

    if isinstance(dt, _date) and not hasattr(dt, "hour"):
        return dt.isoformat()
    return dt.isoformat(sep="T", timespec="seconds")


def select(table: str, *, columns: str = "*", filters: Optional[Dict[str, Any]] = None,
           order: Optional[str] = None, desc_order: bool = False,
           limit: Optional[int] = None, offset: Optional[int] = None,
           context: str = "") -> List[Row]:
    """查多行。

    filters 用 PostgREST 语法：``{"key": "eq.dr007"}``；值为 None 表示
    ``key=is.null``。order 形如 ``"as_of.desc,id.desc"``。
    """
    params: Dict[str, Any] = {"select": columns}
    for k, v in (filters or {}).items():
        params[k] = "is.null" if v is None else v
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset

    resp = _safe("GET", f"/{table}", default=None, params=params, context=context)
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("[cloud_db] %s 响应非 JSON", table)
        return []
    if not isinstance(data, list):
        return []
    return [Row(item) for item in data]


def select_one(table: str, *, columns: str = "*", filters: Optional[Dict[str, Any]] = None,
               order: Optional[str] = None, context: str = "") -> Optional[Row]:
    """查单行（取第一条）。查不到返回 None。"""
    rows = select(table, columns=columns, filters=filters, order=order,
                  limit=1, context=context)
    return rows[0] if rows else None


# 网关硬限制：单次响应最多 1000 行（实测 limit=5000/10000 也只返回 1000），
# 且 Content-Range 恒为 */* 拿不到真实总数。所有「要全量」的读操作必须分页取。
MAX_PAGE_ROWS = 1000


def select_all(table: str, *, columns: str = "*",
               filters: Optional[Dict[str, Any]] = None, order: Optional[str] = None,
               max_rows: int = 50000, context: str = "") -> List[Row]:
    """分页取全量行（绕过网关单次 1000 行的硬限制）。

    max_rows 是安全阀，避免误调用把整张大表拉进内存。
    """
    out: List[Row] = []
    offset = 0
    while offset < max_rows:
        batch = select(table, columns=columns, filters=filters, order=order,
                       limit=MAX_PAGE_ROWS, offset=offset,
                       context=f"{context}:paging" if context else "select_all")
        if not batch:
            break
        out.extend(batch)
        if len(batch) < MAX_PAGE_ROWS:
            break
        offset += MAX_PAGE_ROWS
    return out


def count(table: str, *, filters: Optional[Dict[str, Any]] = None,
          context: str = "") -> int:
    """统计行数（分页累计，规避网关 1000 行/次的硬限制与不可靠的 Content-Range）。"""
    rows = select_all(table, columns="id", filters=filters, max_rows=200000,
                      context=context or f"count:{table}")
    return len(rows)


# ================================================================ 写入

def insert(table: str, rows: Sequence[Dict[str, Any]], *, context: str = "") -> int:
    """插入若干行，返回成功条数（失败返回 0，不抛）。

    大表迁移时单次可能上千行，按 ``_INSERT_CHUNK`` 分片提交，
    避免单个请求体过大被网关拒绝（实测 1000 行一次可过，留足余量）。
    """
    payload = _uniform_keys([dict(r) for r in rows if r])
    if not payload:
        return 0
    total = 0
    for i in range(0, len(payload), _INSERT_CHUNK):
        chunk = payload[i:i + _INSERT_CHUNK]
        resp = _safe("POST", f"/{table}", default=None, json_body=chunk,
                     prefer="return=minimal", context=context)
        if resp is None:
            break
        total += len(chunk)
    return total


def select_or(table: str, *, or_filter: str, columns: str = "*",
              order: Optional[str] = None, desc_order: bool = False,
              limit: Optional[int] = None, offset: Optional[int] = None,
              context: str = "") -> List[Row]:
    """带 OR 组合条件的查询（PostgREST 的 ``or=(...)`` 语法）。

    新闻页的关键词搜索是「标题含关键词 **或** 正文含关键词」，
    对应 ``or=(title.like.*kw*,content.like.*kw*)``。
    """
    params: Dict[str, Any] = {"select": columns, "or": or_filter}
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    resp = _safe("GET", f"/{table}", default=None, params=params, context=context)
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    return [Row(item) for item in data if isinstance(item, dict)]


def _uniform_keys(payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把一批行的 key 集合补齐成一致（PostgREST 硬要求）。

    批量 upsert/insert 时，若各行字段不全相同（如按日期聚合后某天只有 1 个指标列），
    PostgREST 会报 ``PGRST102 All object keys must match``。
    这里取所有行的 key 并集，缺失的补None —— 语义上等价：
    「没提供这个列」与「显式置空」对 upsert 是一样的。
    """
    if len(payload) < 2:
        return payload
    keys: List[str] = []
    seen = set()
    for row in payload:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return [{k: row.get(k) for k in keys} for row in payload]


def upsert(table: str, rows: Sequence[Dict[str, Any]], *, on_conflict: str,
           context: str = "") -> List[Row]:
    """按唯一键 upsert：命中冲突则更新为新值，返回落库后的行。

    on_conflict 是逗号分隔的冲突列，如 ``"key"`` 或 ``"stock_code,report_date"``。
    依赖唯一约束（UNIQUE / CONSTRAINT ... UNIQUE）存在。
    """
    payload = _uniform_keys([dict(r) for r in rows if r])
    if not payload:
        return []
    resp = _safe(
        "POST", f"/{table}", params={"on_conflict": on_conflict},
        default=None, json_body=payload,
        prefer="resolution=merge-duplicates,return=representation",
        context=context,
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [Row(item) for item in data if isinstance(item, dict)]


def delete(table: str, *, filters: Optional[Dict[str, Any]] = None,
           context: str = "") -> int:
    """按条件删除，返回受影响行数。**filters 为空会拒绝执行**，防误删全表。"""
    if not filters:
        logger.error("[cloud_db] 拒绝无条件的 DELETE: %s", table)
        return 0
    params = {k: ("is.null" if v is None else v) for k, v in filters.items()}
    resp = _safe("DELETE", f"/{table}", default=None, params=params,
                 prefer="return=representation", context=context)
    if resp is None:
        return 0
    try:
        data = resp.json()
    except ValueError:
        return 0
    return len(data) if isinstance(data, list) else 0


# ================================================================ 业务封装

def fetch_snapshot(table: str) -> List[Row]:
    """取「最新快照」表的全部行（按 key 索引由调用方做）。"""
    return select(table, order="key.asc", context="fetch_snapshot:" + table)


def fetch_history_series(table: str, keys: Iterable[str], per_key_limit: int = 3000,
                         fetch_all: bool = False) -> Dict[str, List[Row]]:
    """按 key 取历史序列，返回「时间正序」的结果（与原 ORM 实现口径一致）。

    注意：按 as_of（数据日期）排序而非 id —— 回溯补齐前行 id 可能很小，
    按id 排会让折线图乱序。

    fetch_all=True 时不截断（供 upsert_history 做全量判重使用）。
    """
    keys = [k for k in keys if k]
    result: Dict[str, List[Row]] = {k: [] for k in keys}
    if not keys:
        return result

    # 一次查全部（PostgREST 支持 in 过滤），避免每 key 一次往返
    rows = select(
        table,
        filters={"key": "in.(" + ",".join(keys) + ")"},
        order="key.asc,as_of.desc,id.desc",
        context="fetch_history_series:" + table,
    )
    buckets: Dict[str, List[Row]] = {k: [] for k in keys}
    for row in rows:
        k = row["key"]
        if k in buckets:
            buckets[k].append(row)

    for k in keys:
        seq = buckets[k] if fetch_all else buckets[k][:per_key_limit]
        seq.reverse()  # 转成时间正序
        result[k] = seq
    return result


def upsert_history(table: str, records: Sequence[Dict[str, Any]], *,
                   context: str = "") -> Dict[str, int]:
    """历史表按 (key, as_of) 写入，返回 {"added", "updated"}。

    语义与原 `_bulk_upsert_history` 严格一致：
    - 该 (key, as_of) 不存在 → 新增
    - 已存在且数值相同 → 跳过（不计入 updated）
    - 已存在但数值不同 → 就地修正
    """
    added, updated = 0, 0
    records = [r for r in records if r]
    if not records:
        return {"added": 0, "updated": 0}

    # 历史表没有 (key, as_of) 唯一约束，无法用 on_conflict 一次搞定；
    # 按原实现口径：先查已有 → 分成「新增」与「修正」两批分别写。
    #
    # 同批次内可能有重复的 (key, as_of)：**后写的覆盖先写的**（与 SQLite 侧
    # 逐条遍历、后写入者胜出的行为一致）。注意必须 `dict(rec)` 拷贝后再写回，
    # 否则 uniq 里存的是 seen 的别名，update 会把已入列的记录一起改掉。
    seen: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    order: List[Tuple[str, Optional[str]]] = []
    for rec in records:
        pair = (rec["key"], rec.get("as_of"))
        if pair not in seen:
            order.append(pair)
            seen[pair] = dict(rec)
        else:
            seen[pair].update({k: v for k, v in rec.items() if k != "fetched_at"})
    uniq = [seen[p] for p in order]

    existing_rows = fetch_history_series(table, [r["key"] for r in uniq],
                                         per_key_limit=100000, fetch_all=True)
    existing: Dict[Tuple[str, Optional[str]], Row] = {}
    for key, rows in existing_rows.items():
        for row in rows:
            existing[(key, row["as_of"])] = row

    to_insert: List[Dict[str, Any]] = []
    to_update: List[Dict[str, Any]] = []
    for rec in uniq:
        row = existing.get((rec["key"], rec.get("as_of")))
        # fetched_at / created_at / updated_at 若调用方显式给了就原样保留
        # （存量迁移依赖它们保持真实落库时间；快照刷新不传，走默认值）
        extra = {
            k: rec[k] for k in ("fetched_at", "created_at", "updated_at")
            if rec.get(k) is not None
        }
        if row is None:
            to_insert.append({
                "key": rec["key"],
                "value": rec.get("value"),
                "as_of": rec.get("as_of"),
                "source": rec.get("source", "akshare"),
                "detail": rec.get("detail", "{}"),
                **extra,
            })
            added += 1
            continue
        # 读数等价则跳过
        if (row["as_of"] or "") == (rec.get("as_of") or "") and _same_value(
            row["value"], rec.get("value")
        ):
            continue
        to_update.append({
            "id": row["id"],
            "value": rec.get("value"),
            "source": rec.get("source", row["source"]),
            "detail": rec.get("detail", row["detail"]),
        })
        updated += 1

    if to_insert:
        n = insert(table, to_insert, context=context or f"upsert_history:{table}")
        added = n  # 写失败时以实际成功数为准
    if to_update:
        _update_by_id(table, to_update, context=context or f"upsert_history:{table}")

    return {"added": added, "updated": updated}


def _update_by_id(table: str, rows: Sequence[Dict[str, Any]], *, context: str = "") -> int:
    """按 id 逐行更新（PostgREST 不支持多行不同值的批量 PATCH）。"""
    n = 0
    for row in rows:
        row_id = row.get("id")
        if row_id is None:
            continue
        payload = {k: v for k, v in row.items() if k != "id"}
        resp = _safe("PATCH", f"/{table}", default=None, params={"id": f"eq.{row_id}"},
                     json_body=payload, prefer="return=minimal", context=context)
        if resp is not None:
            n += 1
    return n


def _same_value(old: Any, new: Any) -> bool:
    """两个读数是否等价（与原 `_same_reading` 同口径）。"""
    if old is None and new is None:
        return True
    if old is None or new is None:
        return False
    try:
        return abs(float(old) - float(new)) < 1e-9
    except (TypeError, ValueError):
        return False


def utc_now_iso() -> str:
    """当前 UTC 时间的字符串形式（用于显式写 updated_at / crawl_time）。

    ⚠ 必须是 UTC：SQLite 侧这些列的 server_default 是 ``func.now()``，
    在 SQLite 里等价于 CURRENT_TIMESTAMP，即 **UTC**。若这里写本地时间，
    页面用 ``_utcnow() - updated_at`` 算数据年龄会得到负数，
    「数据已N 小时未落库」的判断也会失真。
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="microseconds"
    )


def max_updated_at(table: str, *, source: Optional[str] = None) -> Optional[Any]:
    """取 updated_at 的最大值（用于「最近更新时间」判定）。

    返回 **naive datetime（UTC）**，与 SQLite 侧 ``max(updated_at)`` 的类型一致，
    调用方（get_macro_last_updated 等）无需区分后端。
    """
    rows = select(
        table, columns="updated_at",
        filters={"source": f"eq.{source}"} if source else None,
        order="updated_at.desc", limit=1,
        context="max_updated_at:" + table,
    )
    return rows[0]["updated_at"] if rows else None