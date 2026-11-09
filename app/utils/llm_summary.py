"""今日热点 · LLM 盘面解读（DeepSeek Flash）。

设计取舍
--------
* **只喂两块数据**：/today 顶部的「今日定调」（指数 / 量能 / 赚钱效应 / 涨停跌停 /
  连板 / 情绪温度）和「今日主线」。热股三榜、资金地图、时间轴都不发 —— 那是几十行
  原始条目，既撑大 token，又容易诱导模型去数榜单、编个股故事。
* **按交易日缓存**：结论写回 ``daily_hotspots.payload.llm_summary``，一天一份。
  页面打开只读快照，不调模型；只有点「重新生成」或收盘轮产出新日报时才重新调用。
* **Key 优先级**：环境变量 / .env（随发布打包，重启后仍在）> 页面填写的运行时值。
  页面填写会立刻写进 os.environ 并尝试回写 .env；云沙箱文件系统是临时的，
  回写失败也不影响本次进程内使用（配置接口会如实返回 persisted=false）。

模型：deepseek-flash（DeepSeek-V4.1-Flash），OpenAI 兼容 /chat/completions。
关掉 thinking 是刻意的：盘面解读不需要长链推理，关掉后响应稳定在几秒，
也更可控；真开思考模式很容易顶穿反代 60s 超时。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 A 股盘面复盘分析师。请基于用户提供的结构化盘面数据，写一份简短的当日复盘解读。

硬性要求：
1. 只使用给定数据。禁止编造任何数字、个股、板块、政策或消息；数据里没有的维度不要提。
2. null 表示该指标今天没取到，直接略过或说明「该项数据缺失」，不要猜一个值。
3. 涨用红色语境、跌用绿色语境是 A 股惯例，但输出里不要写颜色词，也不要写 Markdown 符号。
4. 不做买卖建议、不预测点位、不喊目标价。措辞克制，避免「必然」「一定」。
5. 输出严格 JSON 对象，且只输出这个 JSON，不要代码块围栏：
   {"headline": "一句话定调，25~45 字，先说强弱再说结构",
    "points": ["第 1 条：指数与量能", "第 2 条：涨停与情绪", "第 3 条：主线结构与龙头", "第 4 条：持续性与数据缺口"],
    "watch": "一句话，明日观察点或风险提示"}
   points 给 3~4 条，每条 35~75 字，必须包含具体数字。全文简体中文。"""


class LlmError(RuntimeError):
    """可读的调用失败原因，直接给页面展示。"""


# ---------------------------------------------------------------- Key 配置
# 页面填写的 key 存在这里，并同步到 os.environ（配置读取统一走 getenv）。
_runtime_key: str = ""
_key_from_page: bool = False


def get_api_key() -> str:
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def is_configured() -> bool:
    return bool(get_api_key())


def config_view() -> Dict[str, Any]:
    """给页面的配置状态。**绝不返回完整 Key**，只给尾四位掩码。"""
    key = get_api_key()
    hint = ("sk-…" + key[-4:]) if len(key) > 8 else ("已填写" if key else "")
    return {
        "configured": bool(key),
        "model": settings.DEEPSEEK_MODEL,
        "base_url": settings.DEEPSEEK_BASE_URL,
        "key_hint": hint,
        "source": "页面填写（本次进程）" if _key_from_page else ("环境变量 / .env" if key else ""),
        "timeout": settings.DEEPSEEK_TIMEOUT,
    }


def set_api_key(key: str) -> Dict[str, Any]:
    """页面填写 Key：立刻生效，并尽力回写 .env 让重启后仍然有效。"""
    global _runtime_key, _key_from_page
    cleaned = (key or "").strip()
    if not cleaned:
        raise LlmError("API Key 不能为空")
    _runtime_key = cleaned
    _key_from_page = True
    os.environ["DEEPSEEK_API_KEY"] = cleaned
    persisted = _persist_to_dotenv(cleaned)
    logger.info("DeepSeek API Key 已更新（持久化=%s）", persisted)
    view = config_view()
    view["persisted"] = persisted
    return view


def _persist_to_dotenv(key: str) -> bool:
    """回写项目根 .env。云沙箱可能是临时只读挂载，失败就只保进程内有效。"""
    path = settings.BASE_DIR / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    out, replaced = [], False
    for line in lines:
        if line.lstrip().startswith("DEEPSEEK_API_KEY="):
            out.append("DEEPSEEK_API_KEY=" + key)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out += ["", "# 由 /today 页面「设置 Key」写入（勿提交到 git）",
                "DEEPSEEK_API_KEY=" + key]
    try:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- 输入构造
def build_facts(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 payload 裁成给模型的精简事实表。字段用中文名，模型不易误读量纲。"""
    tone = payload.get("tone") or {}
    limit = tone.get("limit") or {}
    turnover = tone.get("turnover") or {}
    index_rows = [
        {"名称": r.get("name"), "收盘": r.get("value"), "涨跌幅%": r.get("change_pct")}
        for r in (tone.get("index") or []) if isinstance(r, dict)
    ]
    themes = []
    for t in (payload.get("themes") or [])[:4]:
        if not isinstance(t, dict):
            continue
        themes.append({
            "方向": t.get("name"),
            "阶段": t.get("stage"),
            "涨停家数": t.get("limit_up"),
            "最高连板": t.get("max_board"),
            "封单亿元": t.get("seal_fund_yi"),
            "相关新闻篇数": t.get("news_count"),
            "连续上榜天数": t.get("days_on_list"),
            "细分行业": [s.get("name") for s in (t.get("sectors") or []) if isinstance(s, dict)],
            "龙头股": [s.get("name") for s in (t.get("lead_stocks") or [])[:4]
                     if isinstance(s, dict)],
        })
    return {
        "交易日": payload.get("market_date"),
        "系统定调": payload.get("verdict"),
        "指数": index_rows,
        "两市成交额亿元": turnover.get("value"),
        "成交额较上一交易日变化亿元": turnover.get("delta"),
        "赚钱效应百分比": tone.get("breadth"),
        "情绪温度": tone.get("temperature"),
        "涨停家数": limit.get("up"),
        "跌停家数": limit.get("down"),
        "炸板家数": limit.get("broken"),
        "封板率百分比": limit.get("seal_rate"),
        "最高连板": limit.get("max_board"),
        "晋级率百分比": limit.get("promotion"),
        "今日主线": themes,
        "本次数据缺口": payload.get("data_gaps") or [],
    }


# ---------------------------------------------------------------- 调用模型
def _parse_content(content: str) -> Dict[str, Any]:
    """容忍模型偶尔包一层 ```json 围栏，或整体不合规时退化为纯文本。"""
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    # 纯文本兜底：不让一次格式抖动变成页面上的「生成失败」
    return {"headline": "", "points": [p.strip() for p in (content or "").split("\n") if p.strip()][:4],
            "watch": ""}


def generate(payload: Dict[str, Any], *, timeout: Optional[int] = None) -> Dict[str, Any]:
    """调 DeepSeek Flash 生成解读，返回可直接落库的 summary。失败抛 LlmError。"""
    key = get_api_key()
    if not key:
        raise LlmError("尚未配置 DeepSeek API Key，请在「设置 Key」里填写")

    facts = build_facts(payload)
    body = {
        "model": settings.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "以下是今日盘面数据（JSON）：\n"
                                        + json.dumps(facts, ensure_ascii=False)},
        ],
        "stream": False,
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": settings.DEEPSEEK_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    url = settings.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    deadline = int(timeout or settings.DEEPSEEK_TIMEOUT)

    try:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json=body,
            timeout=(8, deadline),
        )
    except requests.Timeout:
        raise LlmError(f"模型响应超时（{deadline}s），请稍后重试")
    except requests.RequestException as exc:
        raise LlmError(f"无法连接模型服务：{str(exc)[:160]}")

    if resp.status_code == 401:
        raise LlmError("API Key 无效或已失效，请在「设置 Key」里更换")
    if resp.status_code in (402, 429):
        raise LlmError("DeepSeek 额度不足或触发限流，请检查账户余额后重试")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = ((resp.json() or {}).get("error") or {}).get("message") or ""
        except (ValueError, AttributeError):
            detail = resp.text[:120]
        raise LlmError(f"模型服务返回 {resp.status_code}：{str(detail)[:180]}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"模型响应格式异常：{str(exc)[:120]}")

    parsed = _parse_content(str(content or ""))
    points = [str(p).strip() for p in (parsed.get("points") or []) if str(p).strip()][:5]
    headline = str(parsed.get("headline") or "").strip()
    watch = str(parsed.get("watch") or "").strip()
    if not headline and points:
        headline, points = points[0], points[1:]
    if not headline and not points:
        raise LlmError("模型返回内容为空，请重试")

    return {
        "headline": headline[:200],
        "points": [p[:300] for p in points],
        "watch": watch[:200],
        "model": settings.DEEPSEEK_MODEL,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_date": payload.get("market_date"),
        "total_tokens": usage.get("total_tokens"),
    }


# ---------------------------------------------------------------- 收盘轮自动补一份
# 日报重新生成会覆盖 payload，连带清掉旧总结；这里静默补一份，让第二天打开就有。
# 完全异步 + 全部异常吞掉：AI 总结是锦上添花，绝不能影响日报落库或收盘轮收尾。
_auto_lock = threading.Lock()
_auto_dates: set[str] = set()


def schedule_auto_summary(market_date: str) -> bool:
    if not market_date or not is_configured():
        return False
    with _auto_lock:
        if market_date in _auto_dates:
            return False
        _auto_dates.add(market_date)
    threading.Thread(target=_auto_worker, args=(market_date,), daemon=True,
                     name="hotspot-llm-summary").start()
    return True


def _auto_worker(market_date: str) -> None:
    try:
        # 等日报落库与云库读缓存失效彻底完成，再读回 payload
        time.sleep(2)
        from app import crud
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            record = crud.get_daily_hotspot(db, market_date)
            if not record:
                return
            payload = record["payload"]
            if payload.get("llm_summary"):
                return  # 手动已经生成过，别重复烧 token
            summary = generate(payload, timeout=25)
            crud.save_hotspot_llm_summary(db, market_date, summary)
            logger.info("今日热点 AI 解读已自动生成 %s", market_date)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("今日热点 AI 解读自动生成失败（不影响日报）：%s", exc)
