"""市场贪婪指数（Greed Index）—— V3.6c 口径的合成与展示数据。

与「A 股冷热三层温度计」（app/utils/stock_temp.py）的关系
--------------------------------------------------------
两者**方向一致**（都是越高越热/越贪婪），但成分与窗口不同：

- 综合市场温度计：估值 35% + 情绪 35% + 资金 30%，分位取**近十年**窗口
- 市场贪婪指数：情绪 88% + 估值 12%，分位取**近 5 年**窗口（V3.6c 标称 13 期，实测不可用）

本模块**不新增任何数据抓取**：5 个主成分全部复用已有 stock_temp 指标
（市场宽度 / 全A换手率 / 两市成交额 / 融资余额占流通市值 / 沪深300 PE-TTM），
只做「分位 → 合成 → 判档 → 信号」。

权重（V3.6c 定稿）
------------------
情绪 88%：市场宽度 / 全A换手率 / 两市成交额 / 融资余额占流通市值，各 22%
估值 12%：沪深300 PE-TTM
    4×22% + 12% = 88% + 12% = 100%
**无资金主成分**（新基金发行 / ETF份额等资金项不参与）。
确认层 0 权重：ERP 股债性价比、破净率（估值是慢变量，只在极端区做闸门）。

分位口径
--------
**直接取历史分位（越高越贪婪）**，不做「100 − 分位」的反向换算——
这与综合温度计的方向天然一致，所以两个指针可以并排看、语义相同。

分位定义：分位 = 历史样本中**严格小于**当前值的个数 ÷ 总样本数 × 100

分位来源：基准值 + 逐日累计
------------------------------
用户 2026-10-20 提供了 V3.6c 的**基准分位**（见 GREED_BASE_PERCENTILES），
并明确要求：**「分位先用上面的分位值，有新增数据累计计算」**。

因此分位按下面顺序取，**基准值永远兜底**：

1. **项目历史表累计**：stock_temp_history 里该key 的逐日读数满
   GREED_BASE_MIN_SAMPLES 期后，用实测分位替换基准值 —— 这就是「有新增数据累计计算」。
2. **基准分位**：样本不足时直接用 GREED_BASE_PERCENTILES 的值，并标注
   「基准分位（已累计 N 期）」。
3. **阈值折算**：连基准值都没有的 key（理论上不会发生）用热度分取反。

⚠️ **不再用长历史源算分位**（app/utils/long_history.py）。
那批序列是「指数成交量×均价」近似口径，与基准分位所依据的官方口径不一致，
混用会得出与基准矛盾的结果 —— 实测融资占比的近似序列算出 95% 分位
（读作「杠杆水位历史最高」），而官方口径的真实情况是杠杆正在退潮
（融资余额从 6 月 30204 亿降到 10 月 24825 亿）。
`greed_long_series` 键不再挂载，长历史模块仅作为备查数据保留。

口径边界
--------
卡片读数一律走数据源官方接口，不受分位口径影响。
"""
import math
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 基准分位

# V3.6c 基准分位（用户 2026-10-20 提供，对应 10/19 读数）。
# 这是**分位的起点**，不是永久常量：项目历史表逐日积累到
# GREED_BASE_MIN_SAMPLES 期后，会自动改用实测分位替换它。
GREED_BASE_PERCENTILES: Dict[str, float] = {
    "breadth": 92.3,        # 市场宽度 69.4%
    "turnover_rate": 23.1, # 全A换手率 1.28%
    "turnover_amt": 30.8,  # 两市成交额 14600 亿
    "margin": 15.4,        # 融资余额占流通市值 2.65%（10/19 T+1）
    "hs300_pe": 15.4,      # 沪深300 PE-TTM 12.99 倍
}

# 项目历史表累计到这么多期后，改用实测分位（不再用基准值）
#
# ⚠️ 2026-10-30 由 **20 改为 2**（用户拍板）。起因：五个主成分攒了 7~12 天仍不足
# 20 期，全部回落基准分位，合成结果每天恒为 37.4（0.22×(92.3+23.1+30.8+15.4)
# +0.12×15.4），而同期读数早就不一样了（宽度 69.4%→14.2%、PE 12.99→15.21），
# 指针成了误导性读数。用户选择「先让它动起来」，接受低样本下分位只能取
# 0%/12.5%/50% 这类粗档。
# 已知代价（2026-10-20 实测过，别当新问题）：样本 2~3 期时「严格小于当前值」
# 的个数只能是 0 或 1，分位必然贴 0%/100%，指数会从基准值 37.4 直接跳到 25 附近。
# 缓解手段只在**展示层**：样本 < GREED_LOW_PRECISION_AT 时 mode 标注「精度低」，
# 不改数值、不做加权（加权收缩方案被用户否掉，改之前先问）。
GREED_BASE_MIN_SAMPLES = 2

# 低于这个样本数时，卡片上标注「精度低」——**纯展示标签，不参与任何计算**。
GREED_LOW_PRECISION_AT = 20

# 基准分位的口径说明（展示用，让用户知道这个数字从哪来）
GREED_BASE_NOTE = "V3.6c 基准分位（对应 10/19 读数）"


# ---------------------------------------------------------------- 分位窗口

# 分位窗口 = 项目历史表里有多少期用多少期（上限 GREED_WINDOW 防口径漂移）。
#
# ⚠️ 这里**刻意不用固定窗口**。历史教训（勿回退）：
#   ·近 13 期（V3.6c 标称）→ 当前值恰是窗口最低值，多项分位恒为 0%
#   · 近 5 年 + 近似长序列 → 算出 95% 这类与基准矛盾的值
# 基准值的口径未公开，固定窗口只会与基准打架。
# 既然分位来自「基准值 + 逐日累计」，窗口大小本身没有统计意义，
# 重要的是「样本是自己每天抓的真实读数」—— 故有多少用多少。
GREED_WINDOW = 300

# 样本量低于此数不给实测分位 —— 1 期样本时「严格小于当前值的个数」只能是 0，
# 分位必然是 0%，是伪信号而非精度问题。2 期起至少有两个取值，有基本分辨力。
GREED_MIN_SAMPLES = 2

# 窗口说明（展示用）
GREED_WINDOW_LABEL = "项目历史表逐日累计"


# ---------------------------------------------------------------- 指标清单

# 5 个主成分：直接取历史分位（越高越贪婪）
GREED_MAIN: List[Dict[str, Any]] = [
    {
        "key": "breadth",
        "name": "市场宽度",
        "subtitle": "当日上涨家数 ÷（上涨 + 下跌）家数",
        "layer": "sentiment",
        "layer_name": "情绪",
        "weight": 0.22,
        "meaning": "宽度反映赚钱效应是普涨还是普跌。指数可以靠权重股拉抬，宽度不会骗人。",
        "guide": "宽度分位高 = 大多数股票在涨 = 市场亢奋、贪婪；分位低 = 普跌 = 冷清。",
    },
    {
        "key": "turnover_rate",
        "name": "全 A 换手率",
        "subtitle": "两市成交额 ÷ 两市流通市值",
        "layer": "sentiment",
        "layer_name": "情绪",
        "weight": 0.22,
        "meaning": "剔除市值自然变大因素后的纯活跃度，比成交额更干净的情绪度量。",
        "guide": "换手率分位高 = 交易极度活跃 = 情绪亢奋；分位低 = 地量 = 冷清。",
    },
    {
        "key": "turnover_amt",
        "name": "两市成交额",
        "subtitle": "沪深两市股票成交金额合计",
        "layer": "sentiment",
        "layer_name": "情绪",
        "weight": 0.22,
        "meaning": "市场活跃度最直接的度量，交易所官方口径（非行情商估算）。",
        "guide": "成交额分位高 = 天量 = 亢奋贪婪；分位低 = 地量 = 冷清。",
    },
    {
        "key": "margin",
        "name": "融资余额占流通市值",
        "subtitle": "融资余额（不含融券余额）占两市流通市值比例",
        "layer": "sentiment",
        "layer_name": "情绪",
        "weight": 0.22,
        "meaning": "杠杆资金水位。融资余额越满越贪婪；杠杆退潮往往领先于指数下跌。"
                    "口径为**融资余额**（不含融券余额）—— 融券是空头头寸，"
                    "与「市场贪婪」方向相反，混进两融合计会互相抵消。",
        "guide": "分位高 = 融资杠杆水位偏高 = 贪婪且脆弱；分位低 = 杠杆低迷 = 冷清。"
                 "看趋势比看点位更重要：连续多日回落说明杠杆在退潮，行情持续性存疑。",
    },
    {
        "key": "hs300_pe",
        "name": "沪深300 PE-TTM",
        "subtitle": "沪深300 指数滚动市盈率",
        "layer": "valuation",
        "layer_name": "估值",
        "weight": 0.12,
        "meaning": "宽基指数估值锚。分位越高说明市场越贵、参与者越贪婪。",
        "guide": "PE 分位高 = 估值偏贵、赚钱效应好，容易滋生贪婪；分位低 = 估值便宜、冷清。",
    },
]

# 2 个确认层：权重 0，只在极端区做闸门
#
# 方向说明：ERP 分位高 = 赔率好 = 股票相对债券便宜（**与贪婪反向**）；
# 破净率分位高 = 破净多 = 悲观定价集中（**与贪婪同向**）。
# 所以确认层**只暴露原始分位，不折算成分数、不参与加权合成**，
# 闸门判定按各自方向分别处理（见 build_greed_index）。
GREED_CONFIRM: List[Dict[str, Any]] = [
    {
        "key": "erp",
        "name": "ERP 股债性价比",
        "subtitle": "全 A PE(TTM) 倒数 − 10 年国债收益率",
        "higher_means": "赔率好 · 股票相对债券便宜（与贪婪反向）",
        "deep_note": "分位 < 20：赔率极低 = 股票相对债券毫无性价比，是「贪婪已到泡沫」的确认证据",
        "meaning": "股权风险溢价 = 1/PE − 10 年期国债收益率，衡量买股票相对买国债多拿多少赔率。",
        "guide": "慢变量。分位越低说明股票越贵、越没有性价比，此时贪婪信号最值得警惕。",
        "direction": "inverse",
    },
    {
        "key": "below_na",
        "name": "破净率",
        "subtitle": "破净股数 ÷ 全 A 家数",
        "higher_means": "破净多 · 悲观定价集中（与贪婪反向）",
        "deep_note": "分位 < 20：破净极少 = 市场难找到便宜货，贪婪已充分体现在价格里",
        "meaning": "股价跌破每股净资产的公司占比，是深度低估与悲观定价的集中体现。",
        "guide": "慢变量。破净率极低说明市场已在极度乐观定价，与贪婪指数共振时泡沫特征最明显。",
        "direction": "inverse",
    },
]

GREED_KEYS = [f["key"] for f in GREED_MAIN] + [f["key"] for f in GREED_CONFIRM]
GREED_MAIN_MAP = {f["key"]: f for f in GREED_MAIN}
GREED_CONFIRM_MAP = {f["key"]: f for f in GREED_CONFIRM}

# ---------------------------------------------------------------- 五档区间
#
# 方向与综合温度计**一致**（越高越热越贪婪）：
# 0 = 冰点冷清（机会区）→ 100 = 亢奋贪婪（泡沫风险区）。
# 色带直接沿用综合温度计的 ZONE_COLORS，不倒序。
GREED_ZONES: List[Dict[str, Any]] = [
    {"lo": 0, "hi": 10, "state": "冰点 · 冷清", "signal": "机会区", "action": "分批布局", "tone": "blue"},
    {"lo": 10, "hi": 30, "state": "偏冷", "signal": "关注", "action": "关注、准备资金", "tone": "cyan"},
    {"lo": 30, "hi": 70, "state": "中性", "signal": "无信号", "action": "不决策，只记录", "tone": "lime"},
    {"lo": 70, "hi": 90, "state": "偏热", "signal": "不追加", "action": "不追加新钱", "tone": "orange"},
    {"lo": 90, "hi": 100.01, "state": "亢奋 · 贪婪", "signal": "泡沫风险", "action": "减仓 · 回避追高", "tone": "red"},
]

# 深度确认阈值（泡沫警示）
BUBBLE_MIN = 90.0           # 贪婪指数 > 90
BUBBLE_ERP_MAX = 20.0       # 且 ERP 分位 < 20（赔率极低）

# 5 日移动平均去极值（可选）
SMOOTH_DAYS = 5


# ---------------------------------------------------------------- 分位计算

def greed_percentile(value: Optional[float],
                     series: List[Dict[str, Any]],
                     window: int = GREED_WINDOW,
                     pct_value: Optional[float] = None) -> Tuple[Optional[float], Optional[str]]:
    """value 在历史样本中的分位（0-100），分位越高 = 原始数值越高。

    分位定义：历史样本中**严格小于**当前值的个数 ÷ 总样本数 × 100
    （规格原文；与 stock_temp.percentile_rank 的「小于等于」不同）

    窗口：取最近 `window` 期（默认近 5 年 = 1260 个交易日）。
    ⚠️ **样本不足 window 期时照样算** —— 用户要求「现有值开始积累，
    不用管样本多少」。有多少期用多少期，卡片标注实际期数即可。
    唯独低于 GREED_MIN_SAMPLES（2 期）时不给分位：1 期样本的分位
    只能是 0% 或 100%，是伪信号而非精度问题。

    量纲错配保护
    ------------
    历史上出现过「序列是 A 指标、当前读数却是 B 指标」的错配（两融踩过：
    series 给的是余额亿元、value 给的是占流通市值 %）。这样算出的分位会
    稳定贴着 0 或 100，看着像「极端读数」，实际毫无意义。
    故当当前值落在序列区间 [min, max] 之外时**拒绝给分位**：调用方会把
    当期值一并并进序列，真实数据的当期值必然落在窗口内；
    落到窗口外只可能是量纲错了。真正的「历史新低 / 新高」不会丢。

    pct_value：分位计算用的值，**默认与 value 相同**。
    两融用得到「读数用占比、分位用余额」的口径 —— 这时传入 pct_value=余额，
    序列也传余额序列，量纲自洽；读数（占比%）只用于展示，两者互不干扰。

    返回 (分位, 窗口说明)；样本不足返回 (None, 说明)。
    """
    vals = [x["value"] for x in (series or []) if x.get("value") is not None]
    if value is None or not vals:
        return None, None
    target = value if pct_value is None else pct_value

    windowed = vals[-window:] if len(vals) > window else vals
    if len(windowed) < GREED_MIN_SAMPLES:
        return None, "样本仅 %d 期，不足 %d 期" % (len(windowed), GREED_MIN_SAMPLES)

    lo, hi = min(windowed), max(windowed)
    if target < lo or target > hi:
        return None, ("读数 %.4g 落在序列区间 [%.4g, %.4g] 之外，疑似量纲不一致，已拒绝计算分位"
                      % (target, lo, hi))

    below = sum(1 for v in windowed if v < target)
    pct = round(below / len(windowed) * 100, 1)
    if len(windowed) >= window:
        label = GREED_WINDOW_LABEL
    else:
        label = "近 %d 期（标称 %d 期）" % (len(windowed), window)
    return pct, label


def percentile_sample_size(series: List[Dict[str, Any]], window: int = GREED_WINDOW) -> int:
    """给定序列在窗口内的实际样本期数（用于判断分位精度）。"""
    vals = [x["value"] for x in (series or []) if x.get("value") is not None]
    return min(len(vals), window)


def _finite(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------------------------------------------------------------- 合成

def _zone_of(value: float) -> Optional[Dict[str, Any]]:
    for z in GREED_ZONES:
        if z["lo"] <= value < z["hi"]:
            return z
    return GREED_ZONES[-1]


def build_greed_index(items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """由 build_item 产出的指标字典合成市场贪婪指数。

    参数 items 是 stock_temp.build_response 里已经算好的 {key: item}，本函数只读。
    item 里用到：
      - detail["贪婪分位"] / detail["贪婪分位窗口"] / detail["贪婪分位样本数"]：
        由 stock_temp_refresh 按本模块口径预先算好并落库
      - heat_score：综合温度计口径的单项热度分（越高越热），用作分位不足时的降级折算
      - value / unit / digits / as_of / state：直接透传给页面展示
    """
    factors: List[Dict[str, Any]] = []
    w_usable = 0.0
    w_total = 0.0
    acc = 0.0

    for spec in GREED_MAIN:
        item = items.get(spec["key"]) or {}
        detail = item.get("detail") or {}
        weight = float(spec["weight"])
        w_total += weight

        pct = _finite(detail.get("贪婪分位"))
        heat = _finite(item.get("heat_score"))
        value = _finite(item.get("value"))
        samples = detail.get("贪婪分位样本数")

        mode = ""
        score = None
        window = detail.get("贪婪分位窗口")
        base_pct = GREED_BASE_PERCENTILES.get(spec["key"])
        samples_n = _finite(samples)
        if pct is not None:
            # V3.6c：直接取历史分位（越高越贪婪），不做反向换算
            score = round(pct, 1)
            if samples_n is not None and samples_n < GREED_LOW_PRECISION_AT:
                # 门槛已降到 2 期（2026-10-30），低样本下分位只有 0/12.5/50 这类粗档，
                # 必须在卡片上说清楚，别让读者以为这是精确读数。
                mode = "实测分位（仅 %d 期，精度低）" % int(samples_n)
            else:
                mode = "实测分位（历史表累计）"
        elif base_pct is not None:
            # 基准分位兜底：历史表样本还不够，用用户提供的 V3.6c 基准值。
            # 不是硬编码 —— 样本攒够后上面那个分支会自动接管。
            score = float(base_pct)
            got = int(samples_n) if samples_n else 0
            mode = "基准分位（无历史样本）"
            window = "%s；历史表可比读数仅 %d 期（满 %d 期即转实测分位）" % (
                GREED_BASE_NOTE, got, GREED_BASE_MIN_SAMPLES)
        elif heat is not None:
            # 兜底：连基准值都没有的 key（理论上不会发生），用热度分
            # —— 热度分越高越热，与贪婪分天然同向
            score = round(heat, 1)
            mode = "阈值折算（无基准分位）"
            window = detail.get("贪婪分位窗口") or "无基准分位且无实测样本"
        else:
            mode = "无数据"

        if score is None:
            factors.append({
                "key": spec["key"], "name": spec["name"], "subtitle": spec["subtitle"],
                "layer": spec["layer"], "layer_name": spec["layer_name"],
                "weight": weight, "weight_pct": round(weight * 100, 2),
                "score": None, "percentile": pct, "pct_window": window,
                "samples": samples,
                "contribution": None, "mode": mode,
                "value": value, "unit": item.get("unit", ""),
                "digits": item.get("digits", 2), "as_of": item.get("as_of", "—"),
                "state": item.get("state") or "数据不足",
                "meaning": spec["meaning"], "guide": spec["guide"],
                "base_pct": base_pct,
                "base_min_samples": GREED_BASE_MIN_SAMPLES,
                "low_precision_at": GREED_LOW_PRECISION_AT,
                "tone": "slate",
            })
            continue

        acc += score * weight
        w_usable += weight
        factors.append({
            "key": spec["key"], "name": spec["name"], "subtitle": spec["subtitle"],
            "layer": spec["layer"], "layer_name": spec["layer_name"],
            "weight": weight, "weight_pct": round(weight * 100, 2),
            "score": score, "percentile": pct, "pct_window": window,
            "samples": samples,
            "contribution": round(score * weight, 2), "mode": mode,
            "value": value, "unit": item.get("unit", ""),
            "digits": item.get("digits", 2), "as_of": item.get("as_of", "—"),
            "state": item.get("state") or "—",
            "meaning": spec["meaning"], "guide": spec["guide"],
            "base_pct": base_pct,
            "base_min_samples": GREED_BASE_MIN_SAMPLES,
            "low_precision_at": GREED_LOW_PRECISION_AT,
            "tone": "blue" if spec["layer"] == "valuation" else "amber",
        })

    # 归一化：按可用权重重新分配，缺失项不稀释读数
    value = round(acc / w_usable, 1) if w_usable > 0 else None
    coverage = round(w_usable / w_total * 100, 0) if w_total else 0.0

    zone = _zone_of(value) if value is not None else None

    # ---- 确认层（0 权重，只读分位，不参与合成）
    confirms: List[Dict[str, Any]] = []
    for spec in GREED_CONFIRM:
        item = items.get(spec["key"]) or {}
        detail = item.get("detail") or {}
        pct = _finite(detail.get("贪婪分位"))
        confirms.append({
            "key": spec["key"], "name": spec["name"], "subtitle": spec["subtitle"],
            "percentile": pct, "pct_window": detail.get("贪婪分位窗口"),
            "samples": detail.get("贪婪分位样本数"),
            "higher_means": spec["higher_means"], "deep_note": spec["deep_note"],
            "direction": spec["direction"],
            "meaning": spec["meaning"], "guide": spec["guide"],
            "value": _finite(item.get("value")), "unit": item.get("unit", ""),
            "digits": item.get("digits", 2), "as_of": item.get("as_of", "—"),
        })

    erp = next((c for c in confirms if c["key"] == "erp"), None)
    erp_pct = erp["percentile"] if erp else None

    # ---- 深度确认：泡沫警示（贪婪指数 > 90 且 ERP 分位 < 20）
    deep = None
    if value is not None and value > BUBBLE_MIN:
        hit = erp_pct is not None and erp_pct < BUBBLE_ERP_MAX
        deep = {
            "kind": "bubble",
            "hit": bool(hit),
            "title": "泡沫警示 · 贪婪且无性价比" if hit else "极度贪婪但赔率未到极低",
            "detail": ("同时满足贪婪指数 > 90 且 ERP 分位 < 20%%：股票相对债券已无性价比，"
                       "属泡沫特征，宜减仓、回避追高。") if hit else
                      ("贪婪指数已到极端区（>90），但 ERP 分位仍在 %.1f%%（未低于 20%%）："
                       "赔率尚未极端，可能还有最后一段惯性。" % (erp_pct if erp_pct is not None else float("nan"))),
            "tone": "red",
        }
    elif value is not None and value < 10:
        deep = {
            "kind": "opportunity",
            "hit": True,
            "title": "冰点冷清 · 逆向机会区",
            "detail": "贪婪指数跌破 10：市场极度冷清，按「买冷卖热」的反向指标逻辑，"
                      "这是分批布局的赔率最好区间（仍需结合基本面与政策面判断）。",
            "tone": "blue",
        }

    return {
        "value": value,
        "state": zone["state"] if zone else "数据不足",
        "signal": zone["signal"] if zone else "—",
        "action": zone["action"] if zone else "—",
        "tone": zone["tone"] if zone else "slate",
        "zones": GREED_ZONES,
        "factors": factors,
        "confirms": confirms,
        "coverage": coverage,
        "deep": deep,
        "formula": ("市场贪婪指数 = 0.22 ×（市场宽度 + 全A换手率 + 两市成交额 + 融资余额占流通市值）"
                    " + 0.12 × 沪深300 PE-TTM"),
        "weight_note": "情绪 88%（四项各 22%）+ 估值 12%（PE-TTM）= 100%，无资金主成分",
        "window_note": ("分位取自项目历史表逐日累计（满 %d 期即用实测；样本少于 %d 期时档位很粗，"
                        "卡片会标「精度低」）；还没有可比读数时用 V3.6c 基准值（对应 10/19 读数）兜底"
                        % (GREED_BASE_MIN_SAMPLES, GREED_LOW_PRECISION_AT)),
        "base_note": GREED_BASE_NOTE,
        "smooth_days": SMOOTH_DAYS,
    }


def moving_average(values: List[float], days: int = SMOOTH_DAYS) -> Optional[float]:
    """最近 days 期的简单移动平均（去极值，可选项）。样本不足返回 None。"""
    vals = [_finite(v) for v in (values or [])]
    vals = [v for v in vals if v is not None]
    if len(vals) < days:
        return None
    return round(sum(vals[-days:]) / days, 1)
