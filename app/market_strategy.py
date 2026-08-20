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
from typing import Any, Dict, Optional, Tuple

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


def advance(db: Session, symbol: str, value: float, date: str) -> bool:
    """在数据落库后推进峰值状态。
    
    算法：新高上移 / 回撤达 red_pct 重置 / 否则不变。

    Args:
        db: 数据库会话（已由调用方管理）。
        symbol: 指标代码（如 NASDAQ100）。
        value: 当前最新值。
        date: 当前日期（YYYY-MM-DD）。

    Returns:
        True 表示状态已更新（新高或重置），False 表示无变化。
    """
    strategy = _get_strategy(symbol)
    if strategy is None:
        return False  # 未配置策略，不推进

    state = crud.get_market_strategy_state(db, symbol)
    red_pct = strategy["red_pct"] / 100.0  # 转为小数

    # 首次初始化：以当前值为峰值
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

    return False


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

    if current_value is None or state is None:
        return result

    strategy = _get_strategy(symbol)
    if strategy is None:
        return result

    peak = state.peak_value
    peak_date = state.peak_date

    result["peak_value"] = peak
    result["peak_date"] = peak_date
    result["peak_change_percent"] = round((current_value - peak) / peak * 100, 2)

    # 触发回撤预警当天（重置刚发生）强制红色，之后进入新一轮按正常回撤分级
    if current_date and state.drawdown_date and current_date == state.drawdown_date:
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