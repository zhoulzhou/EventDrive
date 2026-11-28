"use strict";
/**
 * 市场行情峰值回撤策略引擎（移植自 app/market_strategy.py）。
 *
 * 算法：维护持续追踪的峰值，支持「创新高 → 回撤 -red_pct% → 重置」循环。
 * 策略配置独立于代码，在 data/market_strategy.json 中可修改（无需重启）。
 * 峰值状态持久化到 market_strategy_state 表。
 */
const fs = require("fs");
const path = require("path");
const config = require("../config");
const marketRepo = require("../db/market");

const CONFIG_PATH = path.join(config.dataDir, "market_strategy.json");
const DEFAULT_CONFIG = {
  NASDAQ100: { green_pct: 5, red_pct: 10, enabled: true },
};

let _configCache = null;
let _configMtime = 0;

function loadConfig() {
  let mtime = -1;
  try { mtime = fs.statSync(CONFIG_PATH).mtimeMs; } catch (_) { mtime = -1; }
  if (_configCache && mtime === _configMtime) return _configCache;
  try {
    _configCache = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
    _configMtime = mtime;
  } catch (e) {
    if (_configCache === null) {
      console.warn(`[strategy] ${CONFIG_PATH} 不存在或格式错误，使用默认配置:`, e.message);
      try { fs.writeFileSync(CONFIG_PATH, JSON.stringify(DEFAULT_CONFIG, null, 2), "utf8"); }
      catch (we) { console.warn("[strategy] 写默认配置失败:", we.message); }
    }
    _configCache = JSON.parse(JSON.stringify(DEFAULT_CONFIG));
    _configMtime = -1;
  }
  return _configCache;
}

function getStrategy(symbol) {
  const cfg = loadConfig()[symbol];
  if (cfg && cfg.enabled !== false) return cfg;
  return null;
}

function fixedPeak(strategy) {
  if (strategy.fixed_peak_value != null) {
    return [Number(strategy.fixed_peak_value), strategy.fixed_peak_date || null];
  }
  return null;
}

/** 落库后推进峰值状态。返回是否更新。*/
function advance(symbol, value, date) {
  const strategy = getStrategy(symbol);
  if (!strategy || strategy.red_pct == null) return false; // VIX 类只分级不追踪峰值
  let state = marketRepo.getStrategyState(symbol);
  const redPct = strategy.red_pct / 100;

  // 固定峰值播种
  const fixed = fixedPeak(strategy);
  let seeded = false;
  if (fixed) {
    const [fv, fd] = fixed;
    const stale =
      !state ||
      (fd && (state.peak_date || "") < fd) ||
      (fd && state.peak_date === fd && state.peak_value !== fv);
    if (stale) {
      const seedDate = fd || (state ? state.peak_date : date);
      marketRepo.upsertStrategyState({ symbol, peak_value: fv, peak_date: seedDate, drawdown_date: null });
      console.log(`[strategy] ${symbol} 固定峰值播种: 峰值=${fv}, 日期=${seedDate}`);
      state = marketRepo.getStrategyState(symbol);
      seeded = true;
    }
  }

  if (!state) {
    marketRepo.upsertStrategyState({ symbol, peak_value: value, peak_date: date, drawdown_date: null });
    console.log(`[strategy] ${symbol} 初始化: 峰值=${value}, 日期=${date}`);
    return true;
  }

  if (value > state.peak_value) {
    marketRepo.upsertStrategyState({ symbol, peak_value: value, peak_date: date, drawdown_date: null });
    console.log(`[strategy] ${symbol} 创新高: 峰值 ${state.peak_value}→${value}`);
    return true;
  }
  if (value <= state.peak_value * (1 - redPct)) {
    marketRepo.upsertStrategyState({ symbol, peak_value: value, peak_date: date, drawdown_date: date });
    console.log(`[strategy] ${symbol} 回撤 ${redPct * 100}% 触发预警, 重置峰值: ${state.peak_value}→${value}`);
    return true;
  }
  return seeded;
}

function computeDrawdown(symbol, currentValue, state, currentDate) {
  const result = { peak_value: null, peak_date: null, peak_change_percent: null, drawdown_level: "normal" };
  if (currentValue == null) return result;
  const strategy = getStrategy(symbol);
  if (!strategy) return result;
  let peak, peakDate;
  if (state) {
    peak = state.peak_value; peakDate = state.peak_date;
  } else {
    const fixed = fixedPeak(strategy);
    if (!fixed) return result;
    [peak, peakDate] = fixed;
  }
  result.peak_value = peak;
  result.peak_date = peakDate;
  result.peak_change_percent = Math.round(((currentValue - peak) / peak) * 10000) / 100;

  if (currentDate && state && state.drawdown_date && currentDate === state.drawdown_date) {
    result.drawdown_level = "danger";
    return result;
  }
  const drawdown = Math.max(0, (peak - currentValue) / peak);
  const green = (strategy.green_pct || 0) / 100;
  const red = strategy.red_pct / 100;
  if (drawdown < green) result.drawdown_level = "ok";
  else if (drawdown < red) result.drawdown_level = "warn";
  else result.drawdown_level = "danger";
  return result;
}

function computeDrawdownEvents(points, redPct) {
  const events = [];
  let runningPeak = null, runningPeakDate = null;
  const ratio = 1 - redPct / 100;
  for (const p of points) {
    const v = p.value, d = p.date;
    if (v == null || d == null) continue;
    if (runningPeak == null) { runningPeak = v; runningPeakDate = d; continue; }
    if (v > runningPeak) { runningPeak = v; runningPeakDate = d; }
    else if (v <= runningPeak * ratio) {
      events.push({ peak_date: runningPeakDate, peak_value: runningPeak, low_date: d, low_value: v });
      runningPeak = v; runningPeakDate = d;
    }
  }
  return events;
}

function computeLevel(value, thresholds) {
  const result = { drawdown_level: "normal" };
  if (value == null || !thresholds || thresholds.length < 2) return result;
  if (value < thresholds[0]) result.drawdown_level = "ok";
  else if (value < thresholds[1]) result.drawdown_level = "warn";
  else result.drawdown_level = "danger";
  return result;
}

// ---------- 美股交易日历 ----------
function nthWeekday(year, month, weekday, n) {
  const days = [];
  const last = new Date(year, month, 0).getDate();
  for (let d = 1; d <= last; d++) {
    if (new Date(year, month - 1, d).getDay() === (weekday + 1) % 7) days.push(d);
  }
  return days[n - 1];
}
function lastWeekday(year, month, weekday) {
  const days = [];
  const last = new Date(year, month, 0).getDate();
  for (let d = 1; d <= last; d++) {
    if (new Date(year, month - 1, d).getDay() === (weekday + 1) % 7) days.push(d);
  }
  return days[days.length - 1];
}
function easter(year) {
  const a = year % 19, b = Math.floor(year / 100), c = year % 100;
  const d = Math.floor(b / 4), e = b % 4;
  const f = Math.floor((b + 8) / 25), g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4), k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return { year, month, day };
}
function observedHoliday(y, m, d) {
  const wd = new Date(y, m - 1, d).getDay();
  if (wd === 6) return { y, m, d: d - 1 };
  if (wd === 0) return { y, m, d: d + 1 };
  return { y, m, d };
}
function usHolidays(year) {
  const fmt = (o) => `${o.y}-${String(o.m).padStart(2, "0")}-${String(o.d).padStart(2, "0")}`;
  const set = new Set();
  set.add(fmt(observedHoliday(year, 1, 1)));
  set.add(fmt({ y: year, m: 1, d: nthWeekday(year, 1, 0, 3) }));
  set.add(fmt({ y: year, m: 2, d: nthWeekday(year, 2, 0, 3) }));
  const ea = easter(year);
  const gf = new Date(ea.year, ea.month - 1, ea.day);
  gf.setDate(gf.getDate() - 2);
  set.add(fmt({ y: gf.getFullYear(), m: gf.getMonth() + 1, d: gf.getDate() }));
  set.add(fmt({ y: year, m: 5, d: lastWeekday(year, 5, 0) }));
  set.add(fmt(observedHoliday(year, 6, 19)));
  set.add(fmt(observedHoliday(year, 7, 4)));
  set.add(fmt({ y: year, m: 9, d: nthWeekday(year, 9, 0, 1) }));
  set.add(fmt({ y: year, m: 11, d: nthWeekday(year, 11, 3, 4) }));
  set.add(fmt(observedHoliday(year, 12, 25)));
  return set;
}
function nyDateStr() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
}
function isUsMarketOpen() {
  const s = nyDateStr(); // YYYY-MM-DD
  const [y, m, d] = s.split("-").map(Number);
  const wd = new Date(y, m - 1, d).getDay();
  if (wd === 0 || wd === 6) return false;
  return !usHolidays(y).has(s);
}

module.exports = {
  getStrategy,
  advance,
  computeDrawdown,
  computeDrawdownEvents,
  computeLevel,
  isUsMarketOpen,
};
