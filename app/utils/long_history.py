"""**长历史备查数据**（⚠️ 已不参与分位计算，仅保留作交叉校验用）

⚠️ **当前状态：停用**
2026-10-20 起，贪婪指数的分位改为「V3.6c 基准值 + 项目历史表逐日累计」
（见 `greed_index.GREED_BASE_PERCENTILES`），**不再调用本模块**。
`refresh_snapshot` 已移除 `collect_long_series()` 调用，`greed_long_series`
键也不再挂载到 records。

停用原因
--------
本模块的序列是「指数成交量 × 均价」的**近似口径**，与基准分位所依据的官方口径
不一致，混用会得出与基准矛盾的结果 —— 实测融资占比的近似序列算出 **95% 分位**
（读作「杠杆水位历史最高」），而官方口径的真实情况是杠杆正在退潮
（融资余额从 6 月 30204亿降到 10 月 24825亿）。
分位与基准打架时，基准才是可信的一方（基准来自用户给定的 V3.6c 规格）。

保留理由
--------
这些序列对**交叉校验**仍有价值：项目历史表逐日积累到足够期数后，
可以用本模块的官方序列（融资余额）对比卡片分位是否同向。
需要时手动调用 `collect_long_series()` 即可，不要挂回 refresh 主流程。

原设计（已停用）
----------------

============  ==========================  ==========================================
指标           长历史来源                   说明
============  ==========================  ==========================================
turnover_amt  fetch_market_series_bulk    指数成交量×均价近似，2014 年起 2600+ 期
turnover_rate 同上                        成交额 ÷ 近似流通市值，与卡片同口径
margin        两融余额 ÷ 近似流通市值       余额有 2012 年起 3400+ 期，占比靠 bulk 的市值序列
breadth       ❌ 无长历史源               乐咕只给当日快照，只能靠项目历史表逐日积累
============  ==========================  ==========================================

⚠️ 口径注意事项
--------------
1. bulk 的成交额/换手率是**近似值**（指数成交量×均价），锚点日与交易所官方值完全一致
   （实测偏差 0.01%），但历史日期有 1~2% 误差。用于**算分位**（看相对位置）没问题，
   但**不能替代卡片上的官方读数**——卡片读数仍走交易所官网。
2. 两融占比是「余额 ÷ 近似流通市值」，与卡片同口径（都是占流通市值 %），
   但分母是近似值。同样只用于分位。
3. 这些序列**不写入历史表**（避免 2600 行/项的写入压力，也避免与官方口径混在一张表里
   变成「同一个 key 两种量纲」）。它们只在计算分位时用一次，结果（分位数字）落库。

调用时机
--------
refresh_snapshot() 抓完当前读数后调用一次。耗时约 9 秒（2 次新浪指数日线请求 +
1 次交易所锚点请求），远小于 legulegu 限流等待，可接受。
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 长历史起点。5 年窗口只需要 1260 个交易日，2016 年起留足冗余
LONG_HISTORY_START = "2016-01-01"


def _to_series(pairs: List[Tuple[Optional[str], Optional[float]]]) -> List[Dict[str, Any]]:
    """清洗成 [{as_of, value}]（去空、去重、按日期正序）。"""
    bag: Dict[str, float] = {}
    for as_of, value in pairs:
        if not as_of or value is None:
            continue
        bag[as_of] = float(value)
    return [{"as_of": k, "value": bag[k]} for k in sorted(bag)]


def collect_long_series(
    start: str = LONG_HISTORY_START,
) -> Dict[str, List[Dict[str, Any]]]:
    """采集恐慌指数日频分项的长历史序列。

    返回 {指标 key: [{as_of, value}]}，只包含成功采集到的 key。
    任何一项失败都不影响其他项（宁可少一项分位，也不要整个指数算不出来）。
    """
    from app.utils import stock_temp as st

    out: Dict[str, List[Dict[str, Any]]] = {}

    # ---- 成交额 / 换手率 / 近似流通市值：一次 bulk 调用搞定
    try:
        ts, rs, _anchor, mcap_series = st.fetch_market_series_bulk(start=start)
        if ts:
            out["turnover_amt"] = ts
        if rs:
            out["turnover_rate"] = rs
        if mcap_series:
            out["_mcap"] = mcap_series
        logger.info("恐慌长历史: 成交额 %d 期 / 换手率 %d 期 / 近似流通市值 %d 期",
                    len(ts), len(rs), len(mcap_series))
    except Exception as e:
        logger.warning("恐慌长历史: 批量成交额采集失败（成交额/换手率将用项目历史表）: %s", e)

    # ---- 融资余额（官方原始值，交易所口径，2012 年起 3400+ 期）
    #
    # ⚠️ **这一项当前不参与贪婪指数分位计算**（2026-10-20 用户定案）：
    # 用户提供了 V3.6c 的基准分位，并要求「先用基准值，有新增数据累计计算」。
    # 而本模块的序列是**指数近似口径**（成交额/换手率）或**余额原始值**，
    # 与用户基准值所依据的口径不一致，混用会得出 95% 这类与基准矛盾的结果。
    #
    # 保留采集的理由：融资余额的长历史对「分位累计」有潜在价值 ——
    # 一旦项目历史表逐日积累到足够期数，可用它做交叉校验（卡片分位 vs 余额分位
    # 是否同向）。但**不挂到 records**，避免污染分位。
    #
    # 详见 app/utils/greed_index.py 的 GREED_BASE_PERCENTILES 注释。
    try:
        ak = st._ak()
        df = ak.stock_margin_account_info()
        pairs: List[Tuple[Optional[str], Optional[float]]] = []
        for _, r in df.iterrows():
            rz = st._num(r.get("融资余额"))
            if rz is None:
                continue
            d = st._d(r.get("日期"))
            if not d:
                continue
            pairs.append((d, round(rz, 2)))
        balance_series = _to_series(pairs)
        if balance_series:
            out["margin"] = balance_series
            logger.info("贪婪长历史: 融资余额 %d 期（官方口径，仅备查不参与分位）",
                        len(balance_series))
    except Exception as e:
        logger.warning("贪婪长历史: 融资余额采集失败: %s", e)

    # _mcap 是中间量，不作为指标输出
    out.pop("_mcap", None)
    return out


def apply_long_series(
    records: List[Dict[str, Any]],
    long_series: Dict[str, List[Dict[str, Any]]],
) -> int:
    """把长历史挂到 records 上，供 _fill_greed_percentiles 优先使用。

    挂到 `greed_long_series` 键（与 `greed_series` 区分）：
    - greed_series     ：fetch 函数自带的序列（legulegu / 东财整段）
    - greed_long_series：本模块采集的长历史（指数近似口径）
    两者都是「算分位的样本来源」，取更长的那个用。
    """
    n = 0
    for rec in records:
        key = rec.get("key")
        series = long_series.get(key)
        if not series:
            continue
        own = rec.get("greed_series") or []
        # 只有当长历史明显更长时才用（否则保留自带序列，口径更准）
        if len(series) > len(own):
            rec["greed_long_series"] = series
            n += 1
    return n
