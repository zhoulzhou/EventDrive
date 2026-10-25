"""1 年期 AAA 同业存单收益率（日频）—— 资金面温度计的「银行中期负债成本」分项。

数据源：中国货币网（www.chinamoney.com.cn）「收盘收益率曲线 → 同业存单(AAA)」，
即中债估值口径的 AAA 同业存单到期收益率曲线，取 1 年期那一点。

为什么用它：同业存单是银行主动负债的主要工具，1Y AAA 存单收益率≈银行中期负债成本中枢。
存单上行 → 银行揽存压力大、放贷/买债意愿收敛；存单下行 → 负债端压力缓解，
信用供给更容易打开。它替代了原先的「超储率」——后者是季频、要靠下载央行季报 PDF 解析，
既有季度滞后，发布工具又注入不了环境变量，滚动刷新体验很差。

⚠ 口径如实标注：官方并没有日频的「同业存单发行利率」曲线，本项取 AAA 同业存单
1 年期**到期收益率**作代理（发行利率 ≈ 同期限到期收益率 + 一级市场溢价，点差通常几个 bp）。

⚠ 接口硬限制：单页最多 50 行（pageSize 传 100 直接 403），每行是一个期限点（一天约 17 个点），
所以要用 pageNum 翻页才能拼出最近若干交易日；只保留 yearTermStr == 1.0 的那一点。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/ClsYldCurvHis"
_BOND_TYPE = "CYCC000"          # 同业存单(AAA) 收盘收益率曲线
_PAGE_SIZE = 50                 # 硬上限：>50 会被 WAF 403
_MAX_PAGES = 4
_TIMEOUT = 20
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36")

# 打分用 5 日均线（降噪，过滤缴税、跨季、月末脉冲）
MEAN_WINDOW = 5


def _fetch_page(start: date, end: date, page_num: int) -> List[dict]:
    params = {
        "lang": "CN",
        "reference": "1,2,3",
        "bondType": _BOND_TYPE,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "termId": "1",
        "pageNum": str(page_num),
        "pageSize": str(_PAGE_SIZE),
    }
    resp = requests.get(_URL, params=params, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("records") or []


def _parse_1y(records: List[dict]) -> Dict[str, float]:
    """从一页记录里筛出 1 年期（yearTermStr == 1.0）的到期收益率，返回 {日期: 收益率%}。"""
    out: Dict[str, float] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            term = float(rec.get("yearTermStr"))
        except (TypeError, ValueError):
            continue
        if abs(term - 1.0) > 1e-6:
            continue
        day = str(rec.get("newDateValueCN") or rec.get("newDateValue") or "")[:10]
        try:
            value = float(rec.get("maturityYieldStr"))
        except (TypeError, ValueError):
            continue
        if day and value > 0:
            out[day] = value
    return out


def fetch_series(days: int = 20, need: int = MEAN_WINDOW + 1) -> List[Dict[str, Any]]:
    """抓最近 days 天内可拿到的 1Y AAA 存单收益率逐日序列（按日期升序）。

    翻页直到攒够 need 个交易日或翻完 _MAX_PAGES 页；一天都拿不到时抛异常，
    由上层按「该分项缺数据」处理（不会影响其它分项）。
    """
    end = date.today()
    start = end - timedelta(days=max(5, days))
    got: Dict[str, float] = {}
    last_error: Optional[Exception] = None

    for page_num in range(1, _MAX_PAGES + 1):
        try:
            records = _fetch_page(start, end, page_num)
        except Exception as exc:  # 网络 / WAF / 接口改版
            last_error = exc
            break
        if not records:
            break
        got.update(_parse_1y(records))
        if len(got) >= need:
            break

    if not got:
        raise RuntimeError("同业存单收益率抓取失败: %s" % (last_error or "区间内无 1 年期数据"))
    return [{"as_of": d, "value": round(v, 4)} for d, v in sorted(got.items())]


def fetch_ncd_1y(mean_window: int = MEAN_WINDOW) -> Dict[str, Any]:
    """返回可直接落库的读数：value = 5 日日均收益率（%），as_of = 最新交易日。"""
    series = fetch_series(need=mean_window + 1)
    window = series[-mean_window:]
    mean5 = round(sum(p["value"] for p in window) / len(window), 4)
    last = series[-1]
    return {
        "value": mean5,
        "as_of": last["as_of"],
        "source": "chinamoney",
        "detail": {
            "当日值": last["value"],
            "5日均值": mean5,
            "均线样本天数": len(window),
            "曲线": "中债 AAA 同业存单 1Y 到期收益率（中国货币网）",
        },
        "series": series,
    }
