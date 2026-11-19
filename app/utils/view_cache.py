"""页面级物化快照：把「算好的整页 JSON」存进云库，跨沙箱回收复用。

为什么需要
----------
cloud_db 的读缓存是进程内字典。crawl_logs 里 instance_boot 近 14 天有 273 个
间隔，中位数只有 420 秒（7 分钟）——TTL 再长，进程一回收就全清空；写操作
触发的 clear_cache() 也会整体失效。所以单纯「调 TTL」降不下查询次数。

本模块把日更页面的完整响应写成云库 view_snapshots 的一行，读路径变成：
  1) 轻量查询取「数据指纹」（每张来源表取最新 1 行，通常 1~3 行）
  2) 主键点查取快照（1 行）
  3) 指纹一致、版本一致且未超期 → 直接返回 payload
  4) 否则调 builder() 现算并回填

这些查询本身还会命中 cloud_db 的进程内缓存，所以进程存活期间常常是 0 次
云库往返；即使刚被回收，也只需几次轻量点查，而不是原来的 5~10 次重查询。

覆盖面：stock_temp / macro / a_share 三个主页面，以及 stock_temp_series /
macro_series / hotspot_trend 三个「历史序列页」——后者原本每次开页都要按 key
批量扫历史表或拉几十天日报 payload，是资源点里扫行/事务的主要来源，而它们
的数据同样只在定时抓取时变，缓存命中率极高。

ETag 指纹短缓存分两层，都留 STAMP_TTL_SECONDS 秒：
  - (table, col) -> 最新值：跨 view_key 共享，一次开页连发的主页面 + series
    请求只回源查一次共享表；
  - view_key -> 完整指纹：同一页面 60s 内的重复请求连点查都省掉。
写操作由 cloud_db.clear_cache() 按依赖表精确失效，两层同时清，
所以「刷新数据」后 ETag 必然变化，不会把旧页面钉死。

一致性
------
- 指纹变了（抓取落库）→ 下次请求必然重建；写操作后的 clear_cache() 会顺带
  清掉指纹查询的缓存，保证读到新值。
- schema_version 用于代码口径变更时强制重建（改口径请把版本号 +1）。
- max_age_hours 是兜底：指纹长期不变也定期重建。
- 任何异常（表不存在、权限、JSON、网络）都降级回 builder()，页面绝不 500。
- 本地 SQLite 模式（cloud_db_configured() 为 False）直接走 builder，不依赖新表。

为什么不做「写后删快照」
------------------------
cloud_db 的每次写都会调 clear_cache()，若在清缓存时再发 DELETE，会经 _request
再次触发 clear_cache()，形成递归；抓取期还会产生大量删除。指纹比对是纯读，
无副作用、不怕后台线程被回收腰斩，也不需要改云函数与 scheduler。
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from app import cloud_db as _cdb

logger = logging.getLogger(__name__)

T_VIEW = "view_snapshots"
SCHEMA_VERSION = 1
MAX_AGE_HOURS = 30

# ETag 指纹的进程内短缓存（秒）。一次开页会连发多个接口请求、各算一次 ETag，
# 60s 内复用同一次点查结果，省掉重复的 Python 层调用与缓存查找。
# 写操作由 cloud_db.clear_cache() 按依赖表精确失效，不会出现「刷新后 ETag 仍旧」。
STAMP_TTL_SECONDS = 60

# 每个页面的「数据指纹」来源：一组 (表, 列)，各取最新一行后拼接。
# 单源时返回值与旧版完全一致（已有快照的 source_stamp 不用重建）。
#
# 历史序列页用双源，是为了同时捕获两类写：
#   - 快照表 updated_at：每次定时抓取都显式重写，能捕获「就地修正历史读数」；
#   - 历史表 id（自增只增）：能捕获「回填历史」这类只写历史表的操作。
# 只挂其中任意一个都会漏掉另一类写，导致快照/ETag 钉在旧数据上。
_STAMP_SOURCES: Dict[str, tuple] = {
    "stock_temp": (("stock_temp_indicators", "updated_at"),),
    "macro": (("macro_indicators", "updated_at"),),
    "a_share": (("market_prices", "date"),),
    # 趋势/历史序列页：数据只在定时抓取时变，一天最多两次，快照命中率极高
    "stock_temp_series": (
        ("stock_temp_indicators", "updated_at"),
        ("stock_temp_history", "id"),
    ),
    "macro_series": (
        ("macro_indicators", "updated_at"),
        ("macro_indicator_history", "id"),
    ),
    "hotspot_trend": (
        ("daily_hotspots", "updated_at"),
        ("daily_hotspots", "market_date"),
    ),
    # 主题资金：快照表重写（抓新数据）或 AI 归因回填都会改 updated_at，一次抓取内
    # 双写（快照 + llm 回填）靠 updated_at 变化即可捕获，单源足够。
    "theme_capital": (("theme_capital_snapshots", "updated_at"),),
}


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace(" ", "T", 1)).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _stamp_key(view_key: str) -> str:
    return view_key.split(":", 1)[0]


def _stamp_sources(view_key: str) -> tuple:
    return _STAMP_SOURCES.get(_stamp_key(view_key), ())


def _stamp_value(table: str, col: str) -> str:
    """取一张来源表的最新一列；60s 内跨 view_key 共享。

    一次开页会连发主页面与其 series 两个请求，view_key 不同但常常读同一张
    来源表（stock_temp 与 stock_temp_series 都读 stock_temp_indicators）。
    按 (表, 列) 缓存才能真正做到「一次开页只回源一次」。
    """
    now = time.time()
    ck = (table, col)
    with _stamp_ttl_lock:
        item = _stamp_value_cache.get(ck)
        if item and item[0] > now:
            return item[1]
    rows = _cdb.select(table, columns=col, order=f"{col}.desc",
                       limit=1, context="view_cache:stamp")
    value = str(rows[0].get(col) or "") if rows else ""
    with _stamp_ttl_lock:
        _stamp_value_cache[ck] = (now + STAMP_TTL_SECONDS, value)
    return value


def _stamp(view_key: str) -> str:
    """拼出页面指纹，供 get_or_build 判断要不要重建快照。"""
    return "|".join(_stamp_value(t, c) for t, c in _stamp_sources(view_key))


# ---------------- ETag 指纹短缓存 ----------------
# view_key -> (expire_at, stamp, 依赖表名 frozenset)
# (table, col) -> (expire_at, value) —— 表级共享，跨 view_key 复用点查结果
_stamp_ttl_lock = threading.Lock()
_stamp_ttl_cache: Dict[str, tuple] = {}
_stamp_value_cache: Dict[tuple, tuple] = {}


def _stamp_tables(view_key: str) -> frozenset:
    return frozenset(t for t, _ in _stamp_sources(view_key))


def _drop_stamp_ttl(view_key: str) -> None:
    with _stamp_ttl_lock:
        _stamp_ttl_cache.pop(view_key, None)


def invalidate_stamp_cache(table: Optional[str] = None) -> int:
    """写操作后失效指纹短缓存；由 cloud_db.clear_cache(table=...) 调用。

    按依赖表精确清：抓 crawl_logs 不会把页面指纹打回源，写 stock_temp_history
    则只清 stock_temp_series。table 为空表示全清（手动强刷 / 口径变更）。
    """
    with _stamp_ttl_lock:
        if not table:
            n = len(_stamp_ttl_cache)
            _stamp_ttl_cache.clear()
            _stamp_value_cache.clear()
            return n
        victims = [k for k, item in _stamp_ttl_cache.items()
                   if table in item[2]]
        for k in victims:
            _stamp_ttl_cache.pop(k, None)
        for ck in [k for k in _stamp_value_cache if k[0] == table]:
            _stamp_value_cache.pop(ck, None)
        return len(victims)


def _read(view_key: str) -> Optional[Dict[str, Any]]:
    rows = _cdb.select(
        T_VIEW, columns="payload,source_stamp,built_at,schema_version",
        filters={"view_key": f"eq.{view_key}"}, limit=1,
        context="view_cache:read",
    )
    return rows[0].to_dict() if rows else None


def _write(view_key: str, payload: Any, stamp: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    # built_at 要显式写：upsert 的 resolution=merge-duplicates 只更新提供的列，
    # 依赖数据库 DEFAULT 的话重建后时间戳不会刷新，max_age 兜底会误判。
    _cdb.upsert(T_VIEW, [{
        "view_key": view_key,
        "payload": text,
        "source_stamp": stamp,
        "built_at": _utcnow_naive().isoformat(sep=" ", timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
    }], on_conflict="view_key", context="view_cache:write")


def get_or_build(view_key: str, builder: Callable[[], Any],
                 *, max_age_hours: float = MAX_AGE_HOURS) -> Any:
    """命中快照则直接返回，否则调 builder() 现算并回填；异常一律降级。"""
    if not _cdb.cloud_db_configured():
        return builder()
    try:
        stamp = _stamp(view_key)
        row = _read(view_key)
        if row:
            built = _parse_dt(row.get("built_at"))
            fresh = (built is None
                     or built >= _utcnow_naive() - timedelta(hours=max_age_hours))
            if (fresh and row.get("schema_version") == SCHEMA_VERSION
                    and row.get("source_stamp") == stamp
                    and row.get("payload")):
                return json.loads(row["payload"])
        payload = builder()
        # 失败的响应不落快照，免得把错误页缓存 30 小时
        if isinstance(payload, dict) and payload.get("status") != "error":
            _write(view_key, payload, stamp)
            # 刚写入的 built_at 变了，别让 60s 内的 ETag 还停在旧指纹上
            _drop_stamp_ttl(view_key)
        return payload
    except Exception as exc:  # noqa: BLE001 —— 缓存层不能让整页 500
        logger.warning("[view_cache] %s 快照不可用，降级实时计算: %s", view_key, exc)
        return builder()


def peek_stamp(view_key: str) -> str:
    """给 ETag 用的轻量指纹：数据指纹 + 快照构建时间。

    只走 _stamp / _read 两次点查（多数会被 cloud_db 的进程内缓存命中），
    不跑 builder。结果在进程内留 60s，一次开页的多个接口/多个标签页共用；
    写操作会由 clear_cache 精确失效。异常返回空串，让 ETag 退化为「每次 200」。
    """
    if not _cdb.cloud_db_configured():
        return ""
    now = time.time()
    with _stamp_ttl_lock:
        item = _stamp_ttl_cache.get(view_key)
        if item and item[0] > now:
            return item[1]
    try:
        row = _read(view_key)
        built = row.get("built_at") if row else None
        value = "%s|%s" % (_stamp(view_key), _parse_dt(built) or built or "")
    except Exception:  # noqa: BLE001
        return ""
    with _stamp_ttl_lock:
        _stamp_ttl_cache[view_key] = (
            now + STAMP_TTL_SECONDS, value, _stamp_tables(view_key))
    return value


def invalidate(view_key: Optional[str] = None) -> int:
    """手动失效（可选）。view_key 为空则清空全部快照，用于口径变更后强制重建。"""
    if not _cdb.cloud_db_configured():
        return 0
    filters = ({"view_key": f"eq.{view_key}"} if view_key
               else {"built_at": "gte.1970-01-01T00:00:00"})
    invalidate_stamp_cache()
    return _cdb.delete(T_VIEW, filters=filters, context="view_cache:invalidate")
