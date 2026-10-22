"""东方财富概念板块数据源（今日热点页的**首选源**）。

直接调东财 push2 行情接口（无需 Key）：
- /api/qt/clist/get?fs=m:90+t:3   概念板块全量行情
    一次请求即可同时拿到：涨跌幅(f3)、主力净流入(f62)、主力净占比(f184)、
    上涨家数(f104)、下跌家数(f105)、换手率(f8)、领涨股(f128/f136)、总成交额(f6)
- /api/qt/clist/get?fs=b:BKxxxx   某概念板块的成分股行情
    同样一次请求拿到：最新价(f2)、涨跌幅(f3)、成交额(f6)、换手率(f8)、
    市盈率(f9)、最高(f15)、最低(f16)、昨收(f18)、市净率(f23)

为什么不用 akshare 封装：
- akshare 的 stock_board_concept_name_em 走分页（516 个板块要 6 次请求 + 随机延时），
  且不含主力净流入；直连一次拿 100 条 × 6 页更快，字段也更全。
- 沙箱反代硬超时 60 秒，抓取耗时必须可控。

为什么现在能用东财（历史曾不可用）：
- 2026-10-21 实测：本机与云沙箱均可直连 push2.eastmoney.com（HTTP 200）。
  早期记录里「东财对境外机房 IP 返回 502」是当时的机房出口限制，现已不成立。
  仍保留新浪源作为降级兜底（见 app/utils/hot_sector.py）。

⚠ 多 host 轮换：push2 有多个等价域名（79./17./82./push2.），
部分线路对单域名不通，逐个降级直到成功。
"""
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 等价 host，按顺序尝试（首个不通就换下一个）
_HOSTS = [
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "79.push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "91.push2.eastmoney.com",
]

# 概念板块 / 成分股的行情字段
_BOARD_FIELDS = "f2,f3,f6,f8,f12,f14,f20,f62,f104,f105,f128,f136,f140,f184"
_STOCK_FIELDS = "f2,f3,f6,f8,f9,f12,f14,f15,f16,f18,f20,f23"

# 分页：东财 clist 单页上限 100 条
_PAGE_SIZE = 100

# 概念板块总数约 516，6 页足够；留一点余量避免尾部漏页
_MAX_PAGES = 8

# 单次请求超时（秒）
_REQ_TIMEOUT = 12.0

# 单次抓取的总时限（秒）。
# ⚠ 为什么要它：若东财线路是「连得上但不回包」而不是「快速拒绝」，
# 每个请求都要耗满 timeout，翻 6 页 = 6×timeout，会撞上沙箱反代 60 秒硬超时 → 504。
# 这里给整轮抓取设上限，超时即放弃东财、降级到新浪，保证接口一定能返回。
_DEADLINE_SECONDS = 25.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://quote.eastmoney.com/center/boardlist.html",
}


def _clist_url(host: str, *, fs: str, fields: str, page: int, size: int,
               sort_field: str = "f3") -> str:
    """拼 push2 clist 查询串。

    po=1 表示按 sort_field 降序（涨幅榜），fid 为排序字段。
    """
    return (
        f"https://{host}/api/qt/clist/get"
        f"?pn={page}&pz={size}&po=1&np=1&fltt=2&invt=2&fid={sort_field}"
        f"&fs={fs}&fields={fields}"
    )


def _get_json(url: str, timeout: float = _REQ_TIMEOUT) -> Dict[str, Any]:
    """GET 一个 JSON 接口；失败抛异常由调用方降级。"""
    with httpx.Client(timeout=timeout, headers=_HEADERS, trust_env=False) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("东财接口返回非 JSON 对象")
    return data


def _fetch_clist_all(fs: str, fields: str, *, sort_field: str = "f3",
                     deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    """分页拉取 clist 全量数据，逐个 host 降级直到成功。

    任何一个 host 成功拿到第一页就锁定该 host 继续翻页；
    若该 host 后续页失败，则整体换下一个 host 重来（最多试 3 个 host，避免无限重试）。
    deadline 是整轮抓取的绝对截止时间（time.monotonic 基准），超了就抛异常走降级。
    """
    last_error: Optional[Exception] = None

    for host in _HOSTS[:3]:
        try:
            rows: List[Dict[str, Any]] = []
            total = None
            for page in range(1, _MAX_PAGES + 1):
                # 单次请求的超时不能超过整轮剩余时间，否则总时限形同虚设：
                # 请求本身会耗满 timeout，deadline 只在请求之间检查，管不到请求内部。
                timeout = _REQ_TIMEOUT
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"东财抓取超出总时限（{_DEADLINE_SECONDS:.0f}s），转降级")
                    timeout = max(0.5, min(_REQ_TIMEOUT, remaining))

                url = _clist_url(host, fs=fs, fields=fields, page=page, size=_PAGE_SIZE,
                                 sort_field=sort_field)
                payload = _get_json(url, timeout=timeout)
                data = payload.get("data")
                if not data:
                    break
                if total is None:
                    total = int(data.get("total") or 0)
                diff = data.get("diff") or []
                if isinstance(diff, dict):  # 少数情况下 diff 是字典而非列表
                    diff = list(diff.values())
                if not diff:
                    break
                rows.extend(diff)
                if total and len(rows) >= total:
                    break
                # 分页间隔随机 0.2~0.5 秒，避免被限流
                time.sleep(random.uniform(0.2, 0.5))
            if rows:
                logger.info("东财 clist 抓取成功 host=%s rows=%d", host, len(rows))
                return rows
            last_error = ValueError(f"{host} 返回空数据")
        except Exception as e:
            logger.warning("东财 clist host=%s 失败: %s", host, e)
            last_error = e
            continue

    raise RuntimeError(f"东财 clist 全部 host 均不可达: {last_error}")


def fetch_concept_boards(deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    """概念板块全量行情（含主力净流入），已按涨跌幅降序。

    返回的每项是 push2 原始字段的字典，字段含义见模块 docstring。
    """
    return _fetch_clist_all("m:90+t:3", _BOARD_FIELDS, sort_field="f3", deadline=deadline)


def fetch_board_stocks(board_code: str, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    """某概念板块的成分股行情（原始 push2 字段字典），按涨跌幅降序。"""
    return _fetch_clist_all(f"b:{board_code}", _STOCK_FIELDS, sort_field="f3", deadline=deadline)


def probe() -> Dict[str, Any]:
    """诊断用：探测东财接口在本运行环境下的可达性（不落库、只读）。"""
    out = []
    for host in _HOSTS:
        url = _clist_url(host, fs="m:90+t:3", fields="f12,f14,f3", page=1, size=5)
        item = {"host": host}
        try:
            payload = _get_json(url, timeout=15)
            item["status"] = 200
            item["rows"] = len((payload.get("data") or {}).get("diff") or [])
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out.append(item)
    return {"source": "eastmoney", "results": out}
