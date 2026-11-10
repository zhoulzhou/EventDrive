"""指数「±阈值摆动」识别（/a-share 与 /market 共用同一份判读逻辑）。

用户口径（2026-11-10）：上证指数 ±10%、科创50 ±20%，当前值相对运行高低点
回撤或高出达到阈值就显著标识（卡片红底），曲线上把每次跨阈值也标出来，
参照「全球行情」纳斯达克100 的「运行峰值 + 触发后重置」算法。
同日追加：全球行情的纳斯达克100 卡片也改用这里的 ``current``（整段区间
高低点、双向、**不重置**），避免峰值在触发后被重置、明明还在深度回撤却
显示绿色。纳指100 的曲线事件仍走 market_strategy.compute_drawdown_events，
不受本模块影响。

两个页面都调用 ``compute_swing()``，靠 ``pct`` / ``warn_ratio`` / ``ref_labels``
区分阈值与文案：/a-share 用 a_share_swing 配置 + 默认「年内最高/最低」；
/market 用 NASDAQ100 策略的 red_pct、green_pct/red_pct 比例 + 「区间最高/最低」。

这里刻意分成两件事，口径不同、互不混淆：

1) ``events``（步进事件，供曲线标记）
   维护本轮的运行高点/低点；一旦自高点跌 ≥pct% 或自低点涨 ≥pct%，
   记一个事件并**把基准重置为该点**，继续下一轮。与
   ``market_strategy.compute_drawdown_events`` 同构，只是双向。
   于是「每」跨一次阈值都单独留痕，曲线上能画出一段段波段。

2) ``current``（当前状态，供卡片红底）
   拿**当前值**相对整段展示区间的运行最高/最低算偏离：
   达到阈值 → danger（红底）、达到阈值一半 → warn、否则 ok。
   这里**不随事件重置**：用户要的是「当前值处于显著回撤/高出状态」一直看得见；
   若跟着事件重置，触发当天一过红底就消失，反而失去提示意义。
   两个方向同时达阈值时取「越过阈值更多」的那个方向展示。

阈值不写死在代码里：``data/market_strategy.json`` 的 ``a_share_swing`` 段
（与纳指100 策略同一份配置、互不相干），改完无需重启；文件缺失走默认兜底。
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "market_strategy.json"

# 默认阈值（百分数）：上证波动小用 10%，科创50 波动大用 20%
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "SH000001": 10.0,
    "SH000688": 20.0,
}

# 偏离达到阈值的这个比例就进入黄色预警（仅展示层，不影响事件判定）
WARN_RATIO = 0.5

_cfg_cache: Optional[Dict[str, Any]] = None
_cfg_mtime: float = 0.0


def load_config() -> Dict[str, Dict[str, Any]]:
    """读取 a_share_swing 配置（按文件 mtime 短缓存），合并默认值。"""
    global _cfg_cache, _cfg_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = -1.0
    if _cfg_cache is not None and mtime == _cfg_mtime:
        return _cfg_cache

    raw: Dict[str, Any] = {}
    if mtime >= 0:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = (json.load(f) or {}).get("a_share_swing") or {}
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            logger.warning("A 股阈值配置读取失败，使用默认值: %s", exc)

    merged: Dict[str, Dict[str, Any]] = {}
    for symbol, default_pct in DEFAULT_THRESHOLDS.items():
        entry = raw.get(symbol) or {}
        if isinstance(entry, (int, float)):
            entry = {"pct": float(entry)}
        merged[symbol] = {
            "pct": float(entry.get("pct", default_pct) or default_pct),
            "enabled": bool(entry.get("enabled", True)),
        }
    # 允许配置里新增别的 symbol（目前只会是这两个，留给后续扩展）
    for symbol, entry in raw.items():
        if symbol in merged:
            continue
        if isinstance(entry, (int, float)):
            entry = {"pct": float(entry)}
        if isinstance(entry, dict) and entry.get("pct") is not None:
            merged[symbol] = {"pct": float(entry["pct"]),
                              "enabled": bool(entry.get("enabled", True))}

    _cfg_cache = merged
    _cfg_mtime = mtime
    return merged


def threshold_for(symbol: str) -> Optional[float]:
    """返回该指数的摆动阈值（百分数）；未启用/未配置返回 None。"""
    cfg = load_config().get(symbol)
    if not cfg or not cfg["enabled"] or cfg["pct"] <= 0:
        return None
    return cfg["pct"]


def _empty(pct: float) -> Dict[str, Any]:
    return {
        "threshold_pct": pct,
        "events": [],
        "current": {
            "level": "ok", "direction": None, "ref_type": None,
            "ref_date": None, "ref_value": None,
            "deviation_percent": None, "progress": 0.0,
            "remaining_percent": pct, "triggered": False,
        },
        "high": None,
        "low": None,
    }


def compute_swing(points: List[Dict[str, Any]], pct: float,
                  warn_ratio: Optional[float] = None,
                  ref_labels: Optional[Tuple[str, str]] = None) -> Dict[str, Any]:
    """按日线序列计算阈值摆动（两个行情页共用）。

    Args:
        points: 升序的 ``[{"date": "YYYY-MM-DD", "value": float}, ...]``。
        pct: 阈值百分数（10 表示 ±10%）。
        warn_ratio: 偏离达到阈值的这个比例进入 warn；默认 ``WARN_RATIO``（0.5）。
            /market 传 ``green_pct / red_pct``，让分级仍由策略配置驱动。
        ref_labels: ``(高点标签, 低点标签)``，默认「年内最高 / 年内最低」；
            /market 传「区间最高 / 区间最低」，因为那边口径是整段历史区间。

    Returns:
        ``{"threshold_pct", "events", "current", "high", "low"}``。
        events 每项含 ``type``（drawdown/rally）、``ref_type``、
        ``ref_date/ref_value``、``trigger_date/trigger_value``、``change_percent``。
    """
    result = _empty(float(pct))
    if pct <= 0:
        return result
    wr = WARN_RATIO if warn_ratio is None else max(0.0, float(warn_ratio))
    hi_label, lo_label = ref_labels or ("年内最高", "年内最低")

    clean: List[tuple] = []
    for p in points or []:
        d, v = p.get("date"), p.get("value")
        if d is None or v is None:
            continue
        try:
            clean.append((str(d)[:10], float(v)))
        except (TypeError, ValueError):
            continue
    clean.sort(key=lambda x: x[0])
    if not clean:
        return result

    # 边界用「百分比偏差」判定而不是 value <= peak*0.9：
    # 100 * 1.1 = 110.00000000000001，恰好 +10% 的点会被乘法比较漏掉。
    EPS = 1e-9

    # ---- ① 步进事件：运行高低点 + 触发即重置 ----
    peak = trough = clean[0][1]
    peak_date = trough_date = clean[0][0]
    events: List[Dict[str, Any]] = []
    for date, value in clean[1:]:
        if value > peak:
            peak, peak_date = value, date
        if value < trough:
            trough, trough_date = value, date

        hit: Optional[Dict[str, Any]] = None
        # 高点与当前点同日时不可能构成「自高点回撤」，低点同日同理，
        # 这个判断也挡住了重置当天的零偏离自触发。
        if peak_date != date and peak and (peak - value) / peak * 100 >= pct - EPS:
            hit = {
                "type": "drawdown", "ref_type": "高点",
                "ref_date": peak_date, "ref_value": round(peak, 2),
                "trigger_date": date, "trigger_value": round(value, 2),
                "change_percent": round((value / peak - 1) * 100, 2) if peak else 0.0,
            }
        elif trough_date != date and trough and (value - trough) / trough * 100 >= pct - EPS:
            hit = {
                "type": "rally", "ref_type": "低点",
                "ref_date": trough_date, "ref_value": round(trough, 2),
                "trigger_date": date, "trigger_value": round(value, 2),
                "change_percent": round((value / trough - 1) * 100, 2) if trough else 0.0,
            }
        if hit:
            events.append(hit)
            # 触发后把基准移到该点，开启新一轮 —— 这样下一次 ±pct% 会再记一笔
            peak = trough = value
            peak_date = trough_date = date
    result["events"] = events

    # ---- ② 当前状态：相对整段展示区间的运行最高/最低 ----
    hi_date, hi_value = max(clean, key=lambda x: x[1])
    lo_date, lo_value = min(clean, key=lambda x: x[1])
    last_date, last_value = clean[-1]
    result["high"] = {"date": hi_date, "value": round(hi_value, 2)}
    result["low"] = {"date": lo_date, "value": round(lo_value, 2)}

    dd_pct = (last_value / hi_value - 1) * 100 if hi_value else 0.0
    up_pct = (last_value / lo_value - 1) * 100 if lo_value else 0.0
    dd_progress = max(0.0, -dd_pct) / pct
    up_progress = max(0.0, up_pct) / pct

    if dd_progress >= up_progress:
        direction = "drawdown" if dd_progress > 0 else None
        ref_type = hi_label if dd_progress > 0 else None
        ref_date, ref_value = (hi_date, hi_value) if dd_progress > 0 else (None, None)
        deviation, progress = dd_pct, dd_progress
    else:
        direction, ref_type = "rally", lo_label
        ref_date, ref_value = lo_date, lo_value
        deviation, progress = up_pct, up_progress

    level = "danger" if progress >= 1.0 - EPS else ("warn" if progress >= wr else "ok")
    result["current"] = {
        "level": level,
        "direction": direction,
        "ref_type": ref_type,
        "ref_date": ref_date,
        "ref_value": round(ref_value, 2) if ref_value is not None else None,
        "deviation_percent": round(deviation, 2),
        "progress": round(progress, 3),
        "remaining_percent": round(max(0.0, pct - abs(deviation)), 2),
        # 最新交易日恰好也是一个步进触发点（曲线上会重叠，前端据此少画一个标记）
        "triggered": bool(events and events[-1]["trigger_date"] == last_date),
    }
    return result
