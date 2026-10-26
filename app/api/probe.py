"""数据源探测（诊断用）：在**线上沙箱**里直接试跑候选数据源。

为什么需要它：
    乐咕乐股（legulegu）在沙箱出口 IP 上会被风控限流 —— 连续 6~7 次请求即返回 429，
    akshare 把它包装成「页面中未找到 _csrf 令牌」。本地网络出口与沙箱不同，
    本地跑通不代表线上跑通，所以「换源」必须先在沙箱里实测候选源。

用法：
    GET /api/probe/datasource?src=funddb_pe      （需要登录）
    GET /api/probe/datasource?src=all            （逐个跑，总耗时可能超 60s，慎用）

src 取值见 _PROBES。每个探测限时 25 秒（反代硬超时 60s，留足余量）。
本模块只读、不落库、不改任何状态，验证完可以直接删掉。
"""
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, Header, HTTPException, Request

from app.api.login import is_logged_in
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

PROBE_TIMEOUT = 25  # 秒；单个探测的上限


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # 滤掉 NaN
    except (TypeError, ValueError):
        return None


def _df_report(df, derived: Dict[str, Any] = None) -> Dict[str, Any]:
    """把一个 DataFrame 压成「行数 / 列名 / 首尾样本」，便于在 JSON 里看字段口径。"""
    import pandas as pd  # noqa: F401  （仅确认依赖可用）

    if df is None:
        return {"rows": 0, "empty": True}
    rows = int(len(df))
    out: Dict[str, Any] = {
        "rows": rows,
        "columns": [str(c) for c in df.columns],
    }
    if rows:
        head = df.head(2).to_dict("records")
        tail = df.tail(2).to_dict("records")
        out["head"] = [{k: _jsonable(v) for k, v in r.items()} for r in head]
        out["tail"] = [{k: _jsonable(v) for k, v in r.items()} for r in tail]
    if derived:
        out["derived"] = derived
    return out


def _jsonable(v):
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        f = _num(v) if not isinstance(v, str) else None
        if isinstance(v, float) and v != v:
            return None
        return v
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 各源探测

def _p_version() -> Dict[str, Any]:
    import akshare as ak

    return {"akshare": getattr(ak, "__version__", "unknown")}


def _p_legu_pe() -> Dict[str, Any]:
    import akshare as ak

    df = ak.stock_index_pe_lg(symbol="沪深300")
    return _df_report(df)


def _p_legu_breadth() -> Dict[str, Any]:
    import akshare as ak

    df = ak.stock_market_activity_legu()
    return _df_report(df)


def _p_funddb_pe() -> Dict[str, Any]:
    import akshare as ak

    df = ak.index_value_hist_funddb(symbol="沪深300", indicator="市盈率")
    return _df_report(df)


def _p_funddb_pb() -> Dict[str, Any]:
    import akshare as ak

    df = ak.index_value_hist_funddb(symbol="沪深300", indicator="市净率")
    return _df_report(df)


def _p_funddb_snap() -> Dict[str, Any]:
    """韭圈儿「指数估值快照」：一次请求拿全市场指数的 最新PE / 最新PB。

    这是替代 legu PE/PB 的首选（只要当日值，不拉历史序列）。
    """
    import akshare as ak

    df = ak.index_value_name_funddb()
    derived: Dict[str, Any] = {}
    if df is not None and len(df):
        for name in ("沪深300", "中证500"):
            hit = None
            if "指数名称" in df.columns:
                for _, r in df.iterrows():
                    if str(r.get("指数名称") or "").strip() == name:
                        hit = r.to_dict()
                        break
            if hit:
                derived[name] = {
                    k: _jsonable(v)
                    for k, v in hit.items()
                    if k in ("最新PE", "最新PB", "PE分位", "PB分位", "股息率", "更新时间")
                }
    return _df_report(df, derived)


def _p_csindex() -> Dict[str, Any]:
    """中证指数官网估值（官方口径，无反爬 cookie）。"""
    import akshare as ak

    df = ak.stock_zh_index_value_csindex(symbol="000300")
    return _df_report(df)


def _p_spot_em() -> Dict[str, Any]:
    """东财全 A 快照：一次请求同时算出「破净率」和「涨跌家数（市场宽度）」。

    这是替代 legulegu 破净率 + 市场宽度两个接口的核心候选。
    """
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    derived: Dict[str, Any] = {}
    if df is not None and len(df):
        if "市净率" in df.columns:
            pb = [x for x in (_num(v) for v in df["市净率"]) if x is not None]
            below = sum(1 for x in pb if x < 1)
            derived["破净家数"] = below
            derived["有效PB家数"] = len(pb)
            derived["破净率%"] = round(below / len(pb) * 100, 2) if pb else None
        if "涨跌幅" in df.columns:
            chg = [x for x in (_num(v) for v in df["涨跌幅"]) if x is not None]
            up = sum(1 for x in chg if x > 0)
            down = sum(1 for x in chg if x < 0)
            flat = sum(1 for x in chg if x == 0)
            derived["上涨家数"] = up
            derived["下跌家数"] = down
            derived["平盘家数"] = flat
            derived["宽度%"] = round(up / (up + down) * 100, 1) if (up + down) else None
    return _df_report(df, derived)


_PROBES: Dict[str, Callable[[], Dict[str, Any]]] = {
    "version": _p_version,
    "legu_pe": _p_legu_pe,
    "legu_breadth": _p_legu_breadth,
    "funddb_pe": _p_funddb_pe,
    "funddb_pb": _p_funddb_pb,
    "funddb_snap": _p_funddb_snap,
    "csindex": _p_csindex,
    "spot_em": _p_spot_em,
}


def _authorized(request: Request, access_key: str) -> bool:
    """登录 cookie 或云库 access key 任一通过即可。

    允许 access key 是为了**在浏览器之外**（命令行）验证数据源 —— 这不是新开口子：
    该 key 本就是访问云库的凭据，探针只读。本模块验证完即删。
    """
    if access_key and settings.CLOUD_DB_KEY and access_key == settings.CLOUD_DB_KEY:
        return True
    try:
        return bool(is_logged_in(request))
    except Exception:  # noqa: BLE001
        return False


@router.get("/probe/datasource")
async def probe_datasource(
    request: Request,
    src: str = "version",
    x_wb_webapp_access_key: str = Header(None),
):
    """试跑一个候选数据源，返回可用性 / 耗时 / 字段 / 首尾样本。

    src 可选：version（默认）｜legu_pe｜legu_breadth｜funddb_pe｜funddb_pb｜
    csindex｜spot_em｜all（逐个串行，可能超过网关 60s 超时，建议少用）
    """
    if not _authorized(request, x_wb_webapp_access_key):
        raise HTTPException(status_code=401, detail="未登录")

    names: List[str] = list(_PROBES) if src == "all" else [src]
    unknown = [n for n in names if n not in _PROBES]
    if unknown:
        return {"ok": False, "error": f"未知 src: {unknown}", "available": list(_PROBES)}

    results: Dict[str, Any] = {}
    for name in names:
        t0 = time.time()
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(_PROBES[name]), timeout=PROBE_TIMEOUT
            )
            results[name] = {
                "ok": True,
                "cost_s": round(time.time() - t0, 2),
                **(data or {}),
            }
        except asyncio.TimeoutError:
            results[name] = {
                "ok": False,
                "cost_s": round(time.time() - t0, 2),
                "error": f"超时 >{PROBE_TIMEOUT}s",
            }
        except Exception as e:  # noqa: BLE001
            results[name] = {
                "ok": False,
                "cost_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {str(e)[:400]}",
            }

    return {"ok": all(r.get("ok") for r in results.values()), "probes": results}
