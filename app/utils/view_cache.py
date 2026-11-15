"""页面级物化快照：把「算好的整页 JSON」存进云库，跨沙箱回收复用。

为什么需要
----------
cloud_db 的读缓存是进程内字典。crawl_logs 里 instance_boot 近 14 天有 273 个
间隔，中位数只有 420 秒（7 分钟）——TTL 再长，进程一回收就全清空；写操作
触发的 clear_cache() 也会整体失效。所以单纯「调 TTL」降不下查询次数。

本模块把日更页面的完整响应写成云库 view_snapshots 的一行，读路径变成：
  1) 轻量查询取「数据指纹」（业务表最新时间戳 / 日期，1 行）
  2) 主键点查取快照（1 行）
  3) 指纹一致、版本一致且未超期 → 直接返回 payload
  4) 否则调 builder() 现算并回填

这两次查询本身还会命中 cloud_db 的进程内缓存，所以进程存活期间常常是 0 次
云库往返；即使刚被回收，也只需 2 次轻量查询，而不是原来的 5~10 次重查询。

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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from app import cloud_db as _cdb

logger = logging.getLogger(__name__)

T_VIEW = "view_snapshots"
SCHEMA_VERSION = 1
MAX_AGE_HOURS = 30

# 每个页面的「数据指纹」来源：一张小表 + 一列时间/日期，取最新一行。
# 指纹变了说明当天数据已落库，快照必须重建。
_STAMP_SOURCES: Dict[str, tuple] = {
    "stock_temp": ("stock_temp_indicators", "updated_at"),
    "macro": ("macro_indicators", "updated_at"),
    "a_share": ("market_prices", "date"),
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


def _stamp(view_key: str) -> str:
    src = _STAMP_SOURCES.get(_stamp_key(view_key))
    if not src:
        return ""
    table, col = src
    rows = _cdb.select(table, columns=col, order=f"{col}.desc",
                       limit=1, context="view_cache:stamp")
    if not rows:
        return ""
    return str(rows[0].get(col) or "")


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
        return payload
    except Exception as exc:  # noqa: BLE001 —— 缓存层不能让整页 500
        logger.warning("[view_cache] %s 快照不可用，降级实时计算: %s", view_key, exc)
        return builder()


def peek_stamp(view_key: str) -> str:
    """给 ETag 用的轻量指纹：数据指纹 + 快照构建时间。

    只走 _stamp / _read 两次点查（多数会被 cloud_db 的进程内缓存命中），
    不跑 builder。异常返回空串，让 ETag 退化为「每次 200」而不是报错。
    """
    if not _cdb.cloud_db_configured():
        return ""
    try:
        row = _read(view_key)
        built = row.get("built_at") if row else None
        return "%s|%s" % (_stamp(view_key), _parse_dt(built) or built or "")
    except Exception:  # noqa: BLE001
        return ""


def invalidate(view_key: Optional[str] = None) -> int:
    """手动失效（可选）。view_key 为空则清空全部快照，用于口径变更后强制重建。"""
    if not _cdb.cloud_db_configured():
        return 0
    filters = ({"view_key": f"eq.{view_key}"} if view_key
               else {"built_at": "gte.1970-01-01T00:00:00"})
    return _cdb.delete(T_VIEW, filters=filters, context="view_cache:invalidate")
