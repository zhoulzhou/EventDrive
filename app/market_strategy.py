"""市场行情峰值回撤策略引擎。

核心算法（维护一个持续追踪的峰值，支持"创新高 → 回撤 -red_pct% → 重置"循环）：
  1. 当前值 > 峰值 → 峰值上移（新高）
  2. 当前值 <= 峰值 × (1 - red_pct) → 触发回撤，重置：峰值 = 当前值（开始新一轮）
  3. 否则 → 峰值不变，按回撤幅度显示绿/黄/红

策略配置独立于代码，在 data/market_strategy.json 中可随时修改（无需重启）。
峰值状态持久化到 market_strategy_state 表，跨进程/跨重启保持一致。
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app import crud

logger = logging.getLogger(__name__)

# 策略配置文件路径
CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "market_strategy.json"

# 默认策略（配置缺失时的兜底设置）
_DEFAULT_CONFIG = {
    "NASDAQ100": {
        "green_pct": 5,
        "red_pct": 10,
        "enabled": True,
    }
}

# 简单缓存，避免每次 API 请求都读文件
_config_cache: Optional[Dict[str, Any]] = None
_config_mtime: float = 0.0


def _load_config() -> Dict[str, Any]:
    """加载策略配置（带文件变更检测的短缓存）；文件缺失时生成默认配置。"""
    global _config_cache, _config_mtime
    # 已有缓存且文件未变更 → 直接返回
    if _config_cache is not None:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = -1  # 文件被删除，视为已变更
        if mtime == _config_mtime:
            return _config_cache
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
        _config_mtime = os.path.getmtime(CONFIG_PATH)
        logger.info(f"策略配置已加载: {CONFIG_PATH}")
    except (OSError, json.JSONDecodeError) as e:
        # 仅首次告警并落一份默认配置，便于用户直接编辑；后续不再重复告警
        if _config_cache is None:
            logger.warning(f"策略配置 {CONFIG_PATH} 不存在或格式错误，使用并生成默认配置: {e}")
            try:
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_text(json.dumps(_DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as we:
                logger.warning(f"写入默认策略配置失败: {we}")
        _config_cache = dict(_DEFAULT_CONFIG)
        _config_mtime = -1
    return _config_cache


def _get_strategy(symbol: str) -> Optional[Dict[str, Any]]:
    """获取某指标的策略配置，若未启用则返回 None。"""
    cfg = _load_config().get(symbol)
    if cfg and cfg.get("enabled", True):
        return cfg
    return None


def get_strategy(symbol: str) -> Optional[Dict[str, Any]]:
    """公开接口：获取某指标的策略配置（供 API 层判断是否启用回撤事件等），未配置或未启用返回 None。"""
    return _get_strategy(symbol)


def _fixed_peak(strategy: Dict[str, Any]) -> Optional[Tuple[float, Optional[str]]]:
    """若策略配置了固定峰值则返回 (value, date)，否则返回 None。"""
    if "fixed_peak_value" in strategy:
        return float(strategy["fixed_peak_value"]), strategy.get("fixed_peak_date")
    return None


def advance(db: Session, symbol: str, value: float, date: str) -> bool:
    """在数据落库后推进峰值状态。

    算法（动态追踪）：新高上移 / 回撤达 red_pct 重置 / 否则不变。
    若策略配置了 fixed_peak_value / fixed_peak_date，则先以其作为「当前峰值」
    播种（仅当状态缺失、或状态峰值日期早于配置日期、或同日但值不同时生效，
    用于把当前峰值调整为用户指定的值）；播种后继续走动态策略：后续创新高仍
    会上移峰值，回撤达 red_pct 仍会重置开启新一轮。

    Args:
        db: 数据库会话（已由调用方管理）。
        symbol: 指标代码（如 NASDAQ100）。
        value: 当前最新值。
        date: 当前日期（YYYY-MM-DD）。

    Returns:
        True 表示状态已更新，False 表示无变化。
    """
    strategy = _get_strategy(symbol)
    if strategy is None:
        return False  # 未配置策略，不推进

    state = crud.get_market_strategy_state(db, symbol)
    red_pct = strategy["red_pct"] / 100.0  # 转为小数

    # 固定峰值播种：把「当前峰值」调整为配置值（一次性），之后交给动态策略演进
    seeded = False
    fixed = _fixed_peak(strategy)
    if fixed is not None:
        fixed_peak, fixed_date = fixed
        stale = (
            state is None
            or (fixed_date and state.peak_date < fixed_date)
            or (fixed_date and state.peak_date == fixed_date and state.peak_value != fixed_peak)
        )
        if stale:
            seed_date = fixed_date or (state.peak_date if state else date)
            crud.save_market_strategy_state(db, symbol, fixed_peak, seed_date, drawdown_date=None)
            logger.info(f"[{symbol}] 固定峰值播种: 峰值={fixed_peak}, 日期={seed_date}")
            state = crud.get_market_strategy_state(db, symbol)
            seeded = True

    # 首次初始化（未配置固定峰值）：以当前值为峰值
    if state is None:
        crud.save_market_strategy_state(db, symbol, value, date)
        logger.info(f"[{symbol}] 策略初始化: 峰值={value}, 日期={date}")
        return True

    peak = state.peak_value
    peak_date = state.peak_date

    # 1. 新高 → 峰值上移
    if value > peak:
        crud.save_market_strategy_state(db, symbol, value, date, drawdown_date=None)
        logger.info(f"[{symbol}] 创新高: 峰值 {peak}→{value} (日期 {peak_date}→{date})")
        return True

    # 2. 回撤 >= red_pct → 触发预警，重置峰值
    if value <= peak * (1 - red_pct):
        crud.save_market_strategy_state(db, symbol, value, date, drawdown_date=date)
        logger.info(f"[{symbol}] 回撤 {red_pct*100:.0f}% 触发预警, 重置峰值: {peak}→{value} (日期 {date})")
        return True

    # 未触发新高/回撤：若本次发生了播种则视为状态已更新
    return seeded


def compute_drawdown(
    symbol: str, current_value: Optional[float], state: Optional[Any], current_date: Optional[str] = None,
) -> Dict[str, Any]:
    """根据当前值 + 策略状态，计算回撤分级信息。

    Args:
        symbol: 指标代码。
        current_value: 当前最新值（可能为 None）。
        state: MarketStrategyState 对象，或 None。
        current_date: 当前最新值对应的日期（YYYY-MM-DD）。若等于最近回撤触发日，
            则强制显示红色预警（触发 -red_pct% 当天）。

    Returns:
        包含 peak_value / peak_date / peak_change_percent / drawdown_level 的字典。
    """
    result: Dict[str, Any] = {
        "peak_value": None,
        "peak_date": None,
        "peak_change_percent": None,
        "drawdown_level": "normal",
    }

    if current_value is None:
        return result

    strategy = _get_strategy(symbol)
    if strategy is None:
        return result

    # 峰值以持久化状态为准（动态策略演进后的真实峰值）；无状态时退回固定峰值配置
    if state is not None:
        peak = state.peak_value
        peak_date = state.peak_date
    else:
        fixed = _fixed_peak(strategy)
        if fixed is None:
            return result
        peak, peak_date = fixed

    result["peak_value"] = peak
    result["peak_date"] = peak_date
    result["peak_change_percent"] = round((current_value - peak) / peak * 100, 2)

    # 触发回撤预警当天（重置刚发生）强制红色，之后进入新一轮按正常回撤分级
    if current_date and state and state.drawdown_date and current_date == state.drawdown_date:
        result["drawdown_level"] = "danger"
        return result

    drawdown = max(0.0, (peak - current_value) / peak)
    green_pct = strategy["green_pct"] / 100.0
    red_pct = strategy["red_pct"] / 100.0

    if drawdown < green_pct:
        result["drawdown_level"] = "ok"
    elif drawdown < red_pct:
        result["drawdown_level"] = "warn"
    else:
        result["drawdown_level"] = "danger"

    return result


def compute_drawdown_events(points: List[Dict[str, Any]], red_pct: float) -> List[Dict[str, Any]]:
    """扫描历史序列，用运行峰值检测回撤 >= red_pct% 的事件（峰值日期/值 + 低点日期/值）。

    算法与 advance() 的动态追踪一致：出现新高则运行峰值上移；当值相对运行
    峰值回撤达 red_pct% 时记录一个回撤事件，并以当前值开启新一轮追踪。

    Args:
        points: 按日期升序的历史点列表，每项含 date / value（None 会跳过）。
        red_pct: 回撤阈值（百分数，如 10 表示 -10%）。

    Returns:
        事件列表，每项含 peak_date / peak_value / low_date / low_value。
    """
    events: List[Dict[str, Any]] = []
    running_peak: Optional[float] = None
    running_peak_date: Optional[str] = None
    threshold_ratio = 1.0 - red_pct / 100.0

    for p in points:
        value = p.get("value")
        date = p.get("date")
        if value is None or date is None:
            continue
        if running_peak is None:
            running_peak = value
            running_peak_date = date
            continue
        if value > running_peak:
            running_peak = value
            running_peak_date = date
        elif value <= running_peak * threshold_ratio:
            events.append({
                "peak_date": running_peak_date,
                "peak_value": running_peak,
                "low_date": date,
                "low_value": value,
            })
            # 回撤后开启新一轮：以当前低值为新基准
            running_peak = value
            running_peak_date = date
    return events