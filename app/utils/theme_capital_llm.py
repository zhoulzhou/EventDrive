"""主题资金 · 产业链归因（DeepSeek Flash，双轮自检）。

与 llm_summary.py 的分工
------------------------
llm_summary 分析「整个 A 股盘面今天为什么这样」；本模块只回答一个问题：
**算力产业链、人形机器人产业链，今天发生了什么、为什么**。

复用 llm_summary 的 Key 配置与底层 ``_call_model``（同一把运行时 Key、同一个模型），
但用独立的 system prompt 与证据包 —— 喂进去的是两条链的成分股资金榜、分环节净流入、
涨停龙头、相关新闻与近几日净流入趋势，而不是全市场盘面。

输出按交易日缓存到 ``theme_capital_snapshots.payload.llm_summary``。任一链生成失败
只标记该链 status=error，不影响另一链，更不影响快照本身。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.utils import llm_summary as _llm
from app.utils import theme_capital as tc

logger = logging.getLogger(__name__)

SUMMARY_VERSION = 1

SYSTEM_ANALYST = """你是 A 股**产业链资金归因分析师**，不是数据复述员。

用户已经在页面上看到了两条产业链（算力、人形机器人）的当日明细：成分股资金榜、
各细分环节净流入、涨停龙头、相关新闻标题、近几日净流入趋势。点开 AI 卡片是想看到
**规则看不出来的东西**：这两条链今天为什么这样走、是不是同一股力量在驱动、接下来看什么。

对每一条链分别做四件事：
1. 找矛盾：把资金与价格、环节之间、龙头与板块、新闻与盘面对不上之处点出来。
   例：整链净流入但个别环节失血、涨停集中在某环节而资金却流向另一环节、相关新闻很多
   但资金并未流入、连续净流入今天突然转向。
2. 提归因假设：给 2~3 个解释「今天这条链为什么这样」，每个都要引用给定证据里的具体
   字段和数值或新闻标题原文，并写清「出现什么现象就说明这个解释错了」。
3. 捞被忽略的信号：页面分栏陈列但没强调的，如某环节暗中吸金、龙头封单弱、
   资金与新闻方向背离、趋势拐点。
4. 给可证伪的验证清单 + 一句话周期定位（这条链处在启动/扩散/分歧/退潮哪一段）。

必须守住：
- 不得编造数据里没有的数字、个股、新闻、政策；引用数值要与给定值一致，缺失写「数据缺失」。
- 不预测点位、不评级、不给买卖建议，不写「必然」「一定」。
- 禁止「算力链净流入 X 亿」「涨停 Y 家」这类纯复述句；数字只能作为证据嵌在推理里。
- 两条链要**分开归因**，也要在 last 一句点明二者是否同一驱动（如都受 AI 资本开支驱动）。
- 全文简体中文，不写 Markdown 符号。

输出严格 JSON 对象，只输出这个 JSON，不要代码块围栏：
{
 "compute": {
   "signal": "算力链今日最反常的一组信号，30~60字，须点到两个及以上字段",
   "why": "为什么这组信号不寻常，30~70字",
   "hypotheses": [
     {"claim": "一条归因判断，30~65字",
      "evidence": ["证据1：字段名+数值或新闻标题原文", "证据2", "证据3"],
      "counter": "出现什么现象说明此假设不成立，20~55字",
      "confidence": "high|medium|low"}
   ],
   "overlooked": "被忽略但值得留意的信号，30~80字",
   "checks": [{"if": "明日可观测条件", "then": "哪个假设被证实或推翻"}],
   "cycle": "这条链处在什么阶段的一句话定位，25~60字",
   "confidence": "high|medium|low"
 },
 "robot": { 与 compute 同结构 },
 "link": "两条链是否同一驱动、如何相互影响的一句话，25~60字"
}
每条链的 hypotheses 给 2~3 条，checks 给 2~3 条；宁可少给，不要凑数。"""

SYSTEM_REVIEW = """你是这份产业链归因的**审稿人**，唯一目标：把复述彻底删掉，只留真正的推理。

逐链检查：
- 凡只是把某指标换成一句话、没有跨字段关系的（「净流入 X 亿」「涨停 Y 家」）→ 删除，
  或改写成它与另一字段/环节/新闻之间的矛盾或关联。
- 凡 claim 里出现 evidence 没覆盖的数字、个股、消息 → 删除或改标「数据缺失」。
- 凡 evidence 只是重复 claim、没给具体字段值或标题原文 → 换成具体证据。
- 凡 counter / checks 写成「关注后续走势」这类空话 → 换成可观测、可证伪的条件。
- 两条链必须分开、各自成立；删改后某条假设不成立就整条删掉。

输出与草稿**同结构**的 JSON（简体中文），只输出修订后的 JSON，不要代码块围栏，
不要输出审稿意见或解释。"""


class ThemeLlmError(RuntimeError):
    pass


# ---------------------------------------------------------------- 证据包
def _stock_rows(rows: Any, cap: int = 8) -> List[dict]:
    out = []
    for r in (rows or [])[:cap]:
        if not isinstance(r, dict):
            continue
        out.append({"名称": r.get("name"), "环节": r.get("seg"),
                    "涨跌%": r.get("change_pct"), "主力净额亿": r.get("net_inflow_yi"),
                    "主力净占比%": r.get("net_ratio")})
    return out


def build_evidence(payload: Dict[str, Any], history: Optional[List[dict]] = None) -> Dict[str, Any]:
    """把主题资金快照裁成给模型的证据包（约 6~10K 字符）。"""
    chains = payload.get("chains") or {}
    hist = payload.get("history") or {}
    out: Dict[str, Any] = {"交易日": payload.get("market_date"),
                            "资金口径": payload.get("fund_source"),
                            "口径提示": "主力净额=超大单+大单净额；成分股为人工维护的代表股样本，非全市场官方统计",
                            "数据缺口": payload.get("data_gaps") or []}
    for key, label in (("compute", "算力产业链"), ("robot", "人形机器人产业链")):
        c = chains.get(key) or {}
        news = [{"时间": n.get("time"), "分类": n.get("tag"), "标题": n.get("title")}
                for n in (c.get("news") or [])[:10] if isinstance(n, dict)]
        series = (hist.get(key) or [])[-_last_n(hist.get(key)):]
        out[label] = {
            "观察样本数": c.get("stock_count"),
            "全链主力净额亿": c.get("net_inflow_yi"),
            "平均涨跌%": c.get("avg_change_pct"),
            "上涨/下跌家数": [c.get("up_count"), c.get("down_count")],
            "各环节净额亿": [{"环节": b.get("seg"), "净额亿": b.get("net_inflow_yi"),
                             "平均涨跌%": b.get("avg_change_pct"), "家数": b.get("count")}
                            for b in (c.get("by_seg") or []) if isinstance(b, dict)],
            "净流入前列": _stock_rows(c.get("inflow_top")),
            "净流出前列": _stock_rows(c.get("outflow_top")),
            "涨停龙头": [{"名称": l.get("name"), "环节": l.get("seg"),
                        "连板": l.get("board"), "封单亿": l.get("seal_fund_yi"),
                        "炸板次数": l.get("broken_times")}
                       for l in (c.get("leaders") or [])[:8] if isinstance(l, dict)],
            "相关新闻": news,
            "近几日全链净额亿_按日期升序": series,
        }
    out["近几日快照档案"] = history or []
    return out


def _last_n(series: Any) -> int:
    return 12 if series else 0


# ---------------------------------------------------------------- JSON / 归一化
def _parse(content: str) -> Dict[str, Any]:
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
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _text(value: Any, cap: int = 300) -> str:
    return str(value or "").strip()[:cap]


def _normalize_chain(obj: Any) -> Optional[Dict[str, Any]]:
    """把一条链的归因收敛成前端可渲染结构。"""
    if not isinstance(obj, dict):
        return None
    hyps = []
    for h in (obj.get("hypotheses") or [])[:3]:
        if not isinstance(h, dict):
            continue
        claim = _text(h.get("claim"))
        if not claim:
            continue
        hyps.append({
            "claim": claim,
            "evidence": [_text(e, 220) for e in (h.get("evidence") or [])
                         if str(e or "").strip()][:4],
            "counter": _text(h.get("counter"), 220),
            "confidence": _text(h.get("confidence"), 20) or "medium",
        })
    checks = []
    for c in (obj.get("checks") or [])[:3]:
        if not isinstance(c, dict):
            continue
        checks.append({"if": _text(c.get("if"), 220), "then": _text(c.get("then"), 220)})
    out = {
        "signal": _text(obj.get("signal")),
        "why": _text(obj.get("why")),
        "hypotheses": hyps,
        "overlooked": _text(obj.get("overlooked")),
        "checks": checks,
        "cycle": _text(obj.get("cycle")),
        "confidence": _text(obj.get("confidence"), 20) or "medium",
    }
    if not out["signal"] and not hyps:
        return None
    return out


# ---------------------------------------------------------------- 生成
def generate(payload: Dict[str, Any], *, history: Optional[List[dict]] = None,
             timeout: Optional[int] = None) -> Dict[str, Any]:
    """两轮生成两条链的归因，返回可落库结构。失败抛 ThemeLlmError。"""
    key = _llm.get_api_key()
    if not key:
        raise ThemeLlmError("尚未配置 DeepSeek API Key，请在今日热点页或本页「设置 Key」填写")
    evidence = build_evidence(payload, history)
    facts = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    deadline = int(timeout or settings.DEEPSEEK_TIMEOUT)
    per_call = max(15, min(deadline // 2, 22))
    max_tokens = settings.DEEPSEEK_MAX_TOKENS

    first, tok1 = _llm._call_model(
        [{"role": "system", "content": SYSTEM_ANALYST},
         {"role": "user", "content": "以下是两条产业链的原始证据（JSON）：\n" + facts}],
        key, per_call, max_tokens, "首轮")
    draft = _parse(first)
    if not (draft.get("compute") or draft.get("robot")):
        raise ThemeLlmError("模型首轮返回内容为空，请重试")

    reviewed, tokens = True, tok1
    final = draft
    try:
        second, tok2 = _llm._call_model(
            [{"role": "system", "content": SYSTEM_REVIEW},
             {"role": "user", "content": "原始证据（JSON）：\n" + facts
              + "\n\n待审草稿（JSON）：\n"
              + json.dumps(draft, ensure_ascii=False, separators=(",", ":"))}],
            key, per_call, max_tokens, "审稿轮")
        tokens += tok2
        cand = _parse(second)
        if cand.get("compute") or cand.get("robot"):
            final = cand
    except _llm.LlmError as exc:
        logger.warning("主题资金审稿轮失败，降级用初稿：%s", exc)
        reviewed = False

    chains: Dict[str, Any] = {}
    for k, label in (("compute", "算力产业链"), ("robot", "人形机器人产业链")):
        norm = _normalize_chain(final.get(k))
        chains[k] = norm if norm else {
            "signal": "", "why": "", "hypotheses": [], "overlooked": "",
            "checks": [], "cycle": "", "confidence": "low", "status": "error",
        }
    return {
        **chains,
        "link": _text(final.get("link"), 220),
        "model": settings.DEEPSEEK_MODEL,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_date": payload.get("market_date"),
        "total_tokens": tokens or None,
        "version": SUMMARY_VERSION,
        "reviewed": reviewed,
        "evidence_chars": len(facts),
    }


# ---------------------------------------------------------------- 统一入口
_gen_lock = threading.Lock()


def generate_for_date(market_date: Optional[str] = None, *, force: bool = False,
                      timeout: Optional[int] = None) -> Dict[str, Any]:
    """取快照 → 命中缓存直接返回 → 否则两轮生成并写回。不抛异常。"""
    if not _llm.is_configured():
        return {"status": "unconfigured", "message": "尚未配置 DeepSeek API Key"}
    if not _gen_lock.acquire(blocking=False):
        return {"status": "running", "message": "正在生成归因，请稍候"}

    from app import crud as _crud
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        record = _crud.get_theme_capital_snapshot(db, market_date)
        if not record:
            return {"status": "empty", "message": "还没有主题资金快照，先点「刷新数据」"}
        payload = record["payload"]
        date = record["market_date"]
        cached = payload.get("llm_summary")
        if (not force and cached and int(cached.get("version") or 0) >= SUMMARY_VERSION
                and cached.get("data_date") == date):
            return {"status": "ok", "cached": True, "summary": cached, "market_date": date}
        history = _crud.list_hotspot_briefs(db, limit=5)
        summary = generate(payload, history=history, timeout=timeout)
        _crud.save_theme_capital_llm(db, date, summary)
        return {"status": "ok", "cached": False, "summary": summary, "market_date": date}
    except ThemeLlmError as exc:
        logger.warning("主题资金归因生成失败：%s", exc)
        return {"status": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("主题资金归因生成异常：%s", exc, exc_info=True)
        return {"status": "error", "message": f"生成失败：{str(exc)[:200]}"}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        _gen_lock.release()
