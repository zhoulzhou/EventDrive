"""今日热点 · LLM 归因分析（DeepSeek Flash，双轮自检）。

设计取舍
--------
这一版的定位是**分析师，不是复述员**。上一版只把 tone + themes 的 574 字符
聚合指标喂给模型，又要求「每条必须包含具体数字」，结果输出和页面上的规则
定调几乎逐字对应 —— 模型手上没有新材料，除了把数字翻译成句子无事可做。

现在改成三件事：
* **喂原始证据，不喂结论**：新闻标题、连板梯队、首板家数、主线龙头股明细
  （连板数 / 首次封板时间 / 炸板次数 / 封单）、资金地图三榜及其定性标签、
  个股资金流、资金口径、数据缺口，整份日报约 10K 字符全给。规则引擎把
  这些材料分栏陈列，但不会把它们连起来 —— 关联与归因正是大模型的活。
  tone / verdict 仍给一份，但明确标注「页面已展示，禁止单独复述」。
* **跨日档案**：最近 5 个交易日的精简指标，回答「今天处在轮动 / 情绪的哪一段」。
* **两轮**：第一轮出归因草稿，第二轮以审稿人身份逐句删掉复述句、给证据不足的
  降级，只留矛盾识别、跨表关联、可证伪假设。这是防止退回复读机的闸门。

输出按交易日缓存到 ``daily_hotspots.payload.llm_summary``，``version=2``；
旧版（version 缺失或 <2）在页面和接口里都视为过期，引导重新生成。

Key 优先级：环境变量 / .env（随发布打包，重启后仍在）> 页面填写的运行时值。
模型：deepseek-flash（DeepSeek-V4.1-Flash），OpenAI 兼容 /chat/completions。
关掉 thinking 是刻意的：两轮调用要守住反代 60s 超时。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import settings

logger = logging.getLogger(__name__)

SUMMARY_VERSION = 2

SYSTEM_ANALYST = """你是 A 股盘面的**归因分析师**，不是数据复述员。

页面已经用规则引擎展示了所有单一指标（成交额、赚钱效应、涨停家数、主线名、最高板、情绪温度），
用户点开 AI 卡片是想看到**规则看不出来的东西**。只把指标改写成句子的回答算失败。

做这五件事：
1. 找矛盾：把两个及以上字段放在一起，指出互相不协调之处。例：封板率高但晋级率低、
   指数下跌但某板块被标为「暗中建仓」、主线涨停家数多但相关新闻篇数为 0、
   连板梯队高层有票而中层断档、龙头首次封板很晚却几乎没炸板。
2. 提归因假设：对「今天为什么是这些方向在涨 / 资金为什么这样走」给 2~3 个解释，
   结合新闻标题语义、资金标签、连板结构、首次封板时间、炸板次数、细分行业分布。
   每条必须写清支撑它的具体证据（字段名 + 数值，或新闻标题原文），
   以及**出现什么现象就说明这个解释错了**。
3. 捞被忽略的信号：页面分栏陈列但没被强调的，如「暗中建仓」板块、政策新闻与主线不对应、
   首板家数与连板梯队的比例、数据缺口导致哪类结论不可靠。
4. 给可证伪的验证清单：明天的什么数据能证实或推翻假设，用「若…则…」。
5. 定位周期：结合近几日档案，说明今天在主线轮动与情绪变化里处于什么位置、是否发生切换。

允许合理推断（这正是你的价值），但必须守住：
- 不得编造数据里没有的数字、个股、板块、政策、消息；引用数字要与给定值一致，缺失就写「数据缺失」。
- 不预测点位、不评级、不给买卖建议，不写「必然」「一定」。
- 禁止「成交额 X 亿」「涨停 Y 家」「主线在 Z」这类纯复述句；数字只能作为证据嵌在推理里。
- 资金口径不同源时不可直接比较绝对值；注意数据缺口会削弱结论。
- 全文简体中文，不写 Markdown 符号。

输出严格 JSON 对象，且只输出这个 JSON，不要代码块围栏：
{
 "anomaly": {"signal": "最反常的一组信号，40~70字，必须同时点到两个及以上字段",
             "why": "为什么这组信号不寻常，40~80字"},
 "hypotheses": [
   {"claim": "一条归因判断，35~70字",
    "evidence": ["证据1：字段名+数值或新闻标题原文", "证据2", "证据3"],
    "counter": "出现什么现象则说明此假设不成立，25~60字",
    "confidence": "high|medium|low"}
 ],
 "overlooked": "规则页面没强调但值得留意的信号，40~90字",
 "checks": [{"if": "明日可观测的条件", "then": "哪个假设被证实或推翻"}],
 "cycle": "结合近几日档案的一句话定位，35~80字",
 "confidence": "high|medium|low"
}
hypotheses 给 2~3 条，checks 给 2~3 条；宁可少给，不要凑数。"""

SYSTEM_REVIEW = """你是这份盘面解读的**审稿人**，唯一目标：把「复述」彻底删掉，只留下真正的推理。

逐句检查草稿：
- 凡只是把某个指标换成一句话、没有跨字段关系的（「成交额缩量 X 亿」「涨停 Y 家」「主线在 Z」）→ 删除，
  或改写成它与另一字段之间的矛盾 / 关联。
- 凡 claim 里出现 evidence 没覆盖的数字、个股、消息 → 删除或改标「数据缺失」。
- 凡 evidence 只是重复 claim、没给出具体字段值或标题原文 → 换成具体证据。
- 凡 counter / checks 写成「关注市场情绪」这类空话 → 换成可观测、可证伪的条件。
- 保留并突出：矛盾识别、跨表关联、新闻语义与盘面的对应或错位、周期定位。
- 删改后若某条假设不再成立，整条删掉，宁可只剩两条。
- 结果应比初稿更短、更硬。

输出与草稿**同结构**的 JSON（简体中文），只输出修订后的 JSON，不要代码块围栏，
不要输出审稿意见或解释。"""


class LlmError(RuntimeError):
    """可读的调用失败原因，直接给页面展示。"""


# ---------------------------------------------------------------- Key 配置
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
        "mode": "归因分析（全量证据 + 近 5 日 + 双轮自检）",
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


# ---------------------------------------------------------------- 跨日档案
def load_history(db, market_date: str, limit: int = 5) -> List[dict]:
    """取「今天之前」的最近 limit 个交易日精简档案，按日期升序。

    读库失败不该让解读失败 —— 没有历史时模型会明确写「缺少跨日基线」，
    而不是整份卡片报错。
    """
    from app import crud
    try:
        rows = crud.list_hotspot_briefs(db, limit=limit + 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("今日热点跨日档案读取失败（不影响当日归因）：%s", exc)
        return []
    return [r for r in rows if str(r.get("日期") or "") < str(market_date)][-limit:]


# ---------------------------------------------------------------- 证据包
def _board_rows(rows: Any, cap: int = 6) -> List[dict]:
    out = []
    for r in (rows or [])[:cap]:
        if not isinstance(r, dict):
            continue
        out.append({
            "名称": r.get("name"), "大方向": r.get("group") or "",
            "涨跌幅": r.get("change_pct"), "净额亿元": r.get("net_inflow_yi"),
            "净占比": r.get("net_ratio"), "定性": r.get("tag"),
        })
    return out


def _stock_rows(rows: Any, cap: int = 10) -> List[dict]:
    out = []
    for r in (rows or [])[:cap]:
        if not isinstance(r, dict):
            continue
        out.append({"名称": r.get("name"), "涨跌幅": r.get("change_pct"),
                    "净额亿元": r.get("net_inflow_yi")})
    return out


def build_evidence(payload: Dict[str, Any],
                   history: Optional[List[dict]] = None) -> Dict[str, Any]:
    """把整份日报裁成给模型的**原始证据包**（约 8~12K 字符）。

    规则已经算好并展示的 tone / verdict 放在「页面已展示」里，用途是让模型
    知道哪些话不能重复，以及做交叉验证；真正要它分析的是「原始证据」那部分。
    """
    tone = payload.get("tone") or {}
    limit = tone.get("limit") or {}
    turnover = tone.get("turnover") or {}
    boards = payload.get("boards") or {}
    stocks = payload.get("stocks") or {}

    themes = []
    for t in (payload.get("themes") or [])[:4]:
        if not isinstance(t, dict):
            continue
        leads = []
        for s in (t.get("lead_stocks") or [])[:6]:
            if not isinstance(s, dict):
                continue
            leads.append({
                "名称": s.get("name"), "连板": s.get("board"),
                "细分行业": s.get("sector"), "首次封板": s.get("first_seal"),
                "炸板次数": s.get("broken_times"), "封单亿元": s.get("seal_fund_yi"),
            })
        themes.append({
            "方向": t.get("name"), "阶段": t.get("stage"),
            "涨停家数": t.get("limit_up"), "最高连板": t.get("max_board"),
            "封单亿元": t.get("seal_fund_yi"), "相关新闻篇数": t.get("news_count"),
            "连续上榜天数": t.get("days_on_list"),
            "细分行业": [s.get("name") for s in (t.get("sectors") or [])
                        if isinstance(s, dict)][:3],
            "龙头股": leads,
        })

    ladder = []
    for row in (stocks.get("ladder") or [])[:8]:
        if not isinstance(row, dict):
            continue
        ladder.append({
            "连板": row.get("board"), "家数": row.get("count"),
            "个股": [{"名称": x.get("name"), "细分行业": x.get("sector"),
                      "封单亿元": x.get("seal_fund_yi")}
                     for x in (row.get("items") or [])[:8] if isinstance(x, dict)],
        })

    news = []
    for n in (payload.get("timeline") or [])[:8]:
        if not isinstance(n, dict):
            continue
        news.append({"时间": n.get("time"), "分类": n.get("tag"),
                     "标题": n.get("title")})

    fund_source = payload.get("fund_source") or ""
    return {
        "交易日": payload.get("market_date"),
        "资金口径": fund_source,
        "资金口径提示": ("东方财富为主力净额；新浪为全部成交净额，两者绝对值不可比"
                        if "新浪" in fund_source or "东方财富" in fund_source else ""),
        "页面已展示_仅可用于交叉验证_禁止单独复述": {
            "规则定调": payload.get("verdict"),
            "指数": [{"名称": r.get("name"), "收盘": r.get("value"),
                      "涨跌幅": r.get("change_pct")}
                     for r in (tone.get("index") or []) if isinstance(r, dict)],
            "两市成交额亿": turnover.get("value"),
            "成交额变化亿": turnover.get("delta"),
            "赚钱效应": tone.get("breadth"),
            "情绪温度": tone.get("temperature"),
            "涨停": limit.get("up"), "跌停": limit.get("down"),
            "炸板": limit.get("broken"), "封板率": limit.get("seal_rate"),
            "最高连板": limit.get("max_board"), "晋级率": limit.get("promotion"),
        },
        "原始证据": {
            "新闻标题": news,
            "连板梯队": ladder,
            "首板家数": stocks.get("first_board_count"),
            "主线与龙头": themes,
            "资金地图": {
                "行业净流入": _board_rows(boards.get("inflow")),
                "行业净流出": _board_rows(boards.get("outflow")),
                "概念净流入": _board_rows(boards.get("concepts")),
            },
            "个股资金": {
                "净流入": _stock_rows(stocks.get("inflow")),
                "净流出": _stock_rows(stocks.get("outflow")),
            },
            "数据缺口": payload.get("data_gaps") or [],
        },
        "近几个交易日档案_按日期升序": history or [],
        "档案说明": "只含精简指标；最后一条是距今最近的一个交易日。档案为空时请明确写「缺少跨日基线」。",
    }


# ---------------------------------------------------------------- JSON 解析
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
    return {"anomaly": {"signal": "", "why": ""},
            "hypotheses": [{"claim": p.strip(), "evidence": [], "counter": "",
                            "confidence": "low"}
                           for p in (content or "").split("\n") if p.strip()][:3],
            "overlooked": "", "checks": [], "cycle": "", "confidence": "low"}


def _normalize(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把模型输出收敛成前端能直接渲染的结构，顺手裁掉超长文本。"""
    if not isinstance(obj, dict):
        return None

    def text(value, cap=300):
        return str(value or "").strip()[:cap]

    anomaly = obj.get("anomaly")
    if isinstance(anomaly, str):
        anomaly = {"signal": anomaly, "why": ""}
    if not isinstance(anomaly, dict):
        anomaly = {}

    hyps = []
    for h in (obj.get("hypotheses") or [])[:3]:
        if isinstance(h, str):
            h = {"claim": h}
        if not isinstance(h, dict):
            continue
        claim = text(h.get("claim"))
        if not claim:
            continue
        ev = [text(e, 220) for e in (h.get("evidence") or [])
              if str(e or "").strip()][:4]
        hyps.append({
            "claim": claim,
            "evidence": ev,
            "counter": text(h.get("counter"), 220),
            "confidence": text(h.get("confidence"), 20) or "medium",
        })

    checks = []
    for c in (obj.get("checks") or [])[:3]:
        if isinstance(c, str):
            c = {"if": c, "then": ""}
        if not isinstance(c, dict):
            continue
        checks.append({"if": text(c.get("if"), 220),
                       "then": text(c.get("then"), 220)})

    out = {
        "anomaly": {"signal": text(anomaly.get("signal")),
                    "why": text(anomaly.get("why"))},
        "hypotheses": hyps,
        "overlooked": text(obj.get("overlooked")),
        "checks": checks,
        "cycle": text(obj.get("cycle")),
        "confidence": text(obj.get("confidence"), 20) or "medium",
    }
    if not out["anomaly"]["signal"] and not hyps:
        return None
    return out


# ---------------------------------------------------------------- 调用模型
def _call_model(messages: List[dict], key: str, timeout: int,
                max_tokens: int, tag: str) -> Tuple[str, int]:
    """单轮 /chat/completions。错误分类成用户能看懂的话。"""
    body = {
        "model": settings.DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    url = settings.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json=body,
            timeout=(8, timeout),
        )
    except requests.Timeout:
        raise LlmError(f"{tag}响应超时（{timeout}s），请稍后重试")
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
    return str(content or ""), int(usage.get("total_tokens") or 0)


def generate(payload: Dict[str, Any], *, history: Optional[List[dict]] = None,
             timeout: Optional[int] = None) -> Dict[str, Any]:
    """两轮生成归因分析，返回可直接落库的 summary。失败抛 LlmError。

    第一轮出草稿 → 第二轮审稿删复述。审稿轮失败时降级用初稿并标 reviewed=False，
    不让一次格式抖动变成页面上的「生成失败」。
    """
    key = get_api_key()
    if not key:
        raise LlmError("尚未配置 DeepSeek API Key，请在「设置 Key」里填写")

    evidence = build_evidence(payload, history)
    facts_text = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    if len(facts_text) > 60000:
        logger.warning("今日热点证据包偏大：%s 字符", len(facts_text))

    # 两轮都要留余量：40s 预算 → 单轮 20s，最坏 40s + 连接开销，守住反代 60s。
    deadline = int(timeout or settings.DEEPSEEK_TIMEOUT)
    per_call = max(15, min(deadline // 2, 22))
    max_tokens = settings.DEEPSEEK_MAX_TOKENS

    first_content, first_tokens = _call_model(
        [{"role": "system", "content": SYSTEM_ANALYST},
         {"role": "user", "content": "以下是今日盘面的原始证据（JSON）：\n" + facts_text}],
        key, per_call, max_tokens, "首轮")
    draft = _normalize(_parse_content(first_content))
    if not draft:
        raise LlmError("模型首轮返回内容为空，请重试")

    reviewed, tokens = True, first_tokens
    try:
        second_content, second_tokens = _call_model(
            [{"role": "system", "content": SYSTEM_REVIEW},
             {"role": "user", "content": "原始证据（JSON）：\n" + facts_text
              + "\n\n待审草稿（JSON）：\n"
              + json.dumps(draft, ensure_ascii=False, separators=(",", ":"))}],
            key, per_call, max_tokens, "审稿轮")
        tokens += second_tokens
        final = _normalize(_parse_content(second_content)) or draft
    except LlmError as exc:
        logger.warning("审稿轮失败，降级使用初稿：%s", exc)
        reviewed, final = False, draft

    return {
        **final,
        "model": settings.DEEPSEEK_MODEL,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_date": payload.get("market_date"),
        "total_tokens": tokens or None,
        "version": SUMMARY_VERSION,
        "reviewed": reviewed,
        "evidence_chars": len(facts_text),
        "history_days": len(history or []),
    }


# ---------------------------------------------------------------- 收盘轮自动补一份
# 日报重新生成会覆盖 payload，连带清掉旧总结；这里静默补一份。
# 完全异步 + 全部异常吞掉：AI 分析是锦上添花，绝不能影响日报落库或收盘轮收尾。
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
            existing = payload.get("llm_summary") or {}
            if int(existing.get("version") or 1) >= SUMMARY_VERSION:
                return  # 手动已经生成过新版，别重复烧 token
            history = load_history(db, market_date)
            summary = generate(payload, history=history, timeout=36)
            crud.save_hotspot_llm_summary(db, market_date, summary)
            logger.info("今日热点 AI 归因分析已自动生成 %s", market_date)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("今日热点 AI 归因分析自动生成失败（不影响日报）：%s", exc)
