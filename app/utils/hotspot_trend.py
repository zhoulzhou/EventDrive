"""热点趋势（/hotspot-trend）的数据整形。

纯函数：输入 ``crud.list_hotspot_reports()`` 返回的按日升序主线，输出直接给
ECharts 用的「行业 × 交易日」点阵。不碰数据库 —— 造几份假日报就能验证排序、
连续天数和空态，不用起服务也不用登录。

两种粒度（页面上可切换）：
  ``group``  —— 今日主线的大方向（涨停股行业经 SECTOR_GROUPS 归并后的名字，如「电子」）
  ``sector`` —— 主线里贡献涨停最多的细分行业（东财二级行业，如「半导体」）

纵轴行业顺序：**关注方向固定在顶部**（半导体 → 计算机 → 人形机器人，顺序即组间
顺序），其余按累计上榜次数降序 → 最近一次上榜日期 → 名称。组内仍是反复出现的老
主线在上，横着看每一行的圆点就是在看这个行业的生命周期。

与今日热点页的关系：**只读同一份 daily_hotspots 快照**，不重新抓取、不新建表，
所以历史从该表开始有数据的那天算起，不会回填更早的交易日。

⚠ 口径与 /today 的「今日主线」严格对齐：日报里最多有 4 条达标方向，但今日热点页
只展示前 3 条（today.js 的 THEME_LIMIT）。本页**每个交易日也只取前 3 条**，
点阵、纵轴行业、最新交易日主线、持续热点排行、统计条全部同源 —— 否则图上
最新一列会多出一个 /today 根本看不到的行业。截断发生在整形层，不动日报快照，
其它调用方拿到的仍是完整 payload。
"""

from typing import Any, Dict, List

# 每日主线展示上限，必须与 static/js/today.js 的 THEME_LIMIT 保持一致。
# 后端日报最多产出 4 条（utils/hotspot.py 的 _build_themes），第 4 条不在两页展示。
THEME_LIMIT = 3

# 每条主线里前 3 个细分行业（crud 已裁过），避免细分视角炸成几十行。
_TOP_RANKING = 8

# 关注方向：用户指定固定在纵轴顶部，元组顺序即组间顺序。
# 行业名来自两套口径 —— 主线方向（半导体/电子/计算机/机械…）和东财二级行业
# （被截断到 4 字，如「计算机设」「自动化设」「光学光电」），所以关键词同时写
# 完整名与截断名，匹配用「任一边包含」兜住截断写法。
# 注意：只做展示排序，不参与上榜口径、不改 ranking 的持续性排名。
FOCUS_GROUPS: tuple = (
    ("半导体", (
        "半导体", "芯片", "集成电路", "分立器件", "光刻", "封测", "晶圆", "存储",
        "元件", "光学光电", "光学光电子", "其他电子", "消费电子", "电子化学品",
        "电子", "PCB", "面板",
    )),
    ("计算机", (
        "计算机", "软件", "IT服务", "信创", "数据要素", "AI应用", "智能体",
        "大模型", "算力", "云计", "通信设备", "通信服务", "光模块", "CPO", "IDC",
    )),
    ("人形机器人", (
        "人形", "机器人", "机械", "自动化设", "通用设备", "专用设备", "减速器",
        "伺服", "电机", "工控", "传感器", "丝杠", "灵巧手", "执行器", "机床",
    )),
)


def _focus_label(name: str) -> str:
    """行业属于哪个关注方向；不属于返回空串。按 FOCUS_GROUPS 顺序取首个命中。"""
    if not name:
        return ""
    for label, keys in FOCUS_GROUPS:
        for key in keys:
            if key in name or name in key:
                return label
    return ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _max_streak(indices: List[int]) -> int:
    """按「有日报的相邻交易日」算最长连续上榜天数（跳过休市日）。"""
    ordered = sorted(set(indices))
    if not ordered:
        return 0
    best = cur = 1
    for i in range(1, len(ordered)):
        cur = cur + 1 if ordered[i] == ordered[i - 1] + 1 else 1
        best = max(best, cur)
    return best


def _order_industries(stats: Dict[str, Dict[str, Any]], pin: bool = True) -> List[str]:
    """pin=True 给点阵纵轴用（关注方向置顶）；pin=False 给持续性排行用，
    避免「持续热点」被展示置顶逻辑带偏，仍按累计上榜次数取前几名。"""
    group_order = {label: i for i, (label, _) in enumerate(FOCUS_GROUPS)}
    resting = len(group_order)

    def sort_key(kv):
        name, s = kv
        focus = _focus_label(name) if pin else ""
        # 精确叫「半导体」「计算机」的行压在「电子」「元件」等相关行前面，
        # 否则按最近上榜排序会把归并名顶到点名方向之前。
        exact = 0 if focus and name == focus else 1
        return (group_order.get(focus, resting), exact,
                -s["count"], -s["last"], name)

    return [name for name, _ in sorted(stats.items(), key=sort_key)]


def _sector_text(theme: Dict[str, Any]) -> str:
    parts = []
    for sec in theme.get("sectors") or []:
        name = str(sec.get("name") or "").strip()
        if name:
            parts.append("%s %s" % (name, _int(sec.get("count"))))
    return " / ".join(parts)


def build_trend(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把按日升序的日报列表整形成趋势页响应。空列表也返回结构完整的空态。"""
    dates: List[str] = []
    for report in reports or []:
        date = str((report or {}).get("market_date") or "").strip()
        if date and date not in dates:
            dates.append(date)
    date_index = {d: i for i, d in enumerate(dates)}
    latest_date = dates[-1] if dates else None

    group_stats: Dict[str, Dict[str, Any]] = {}
    sector_stats: Dict[str, Dict[str, Any]] = {}
    group_points: List[Dict[str, Any]] = []
    sector_points: List[Dict[str, Any]] = []
    latest: List[Dict[str, Any]] = []
    seen_group = set()
    seen_sector = set()
    no_theme_days = 0

    def bump(stats: Dict[str, Dict[str, Any]], name: str, idx: int, count: int) -> None:
        s = stats.setdefault(name, {"count": 0, "last": -1, "best": 0, "indices": []})
        s["count"] += 1
        s["last"] = max(s["last"], idx)
        s["best"] = max(s["best"], count)
        s["indices"].append(idx)

    for report in reports or []:
        date = str((report or {}).get("market_date") or "").strip()
        if date not in date_index:
            continue
        idx = date_index[date]
        themes = ((report or {}).get("themes") or [])[:THEME_LIMIT]
        if not themes:
            no_theme_days += 1
        if date == latest_date:
            latest = []
        for rank, theme in enumerate(themes, 1):
            name = str(theme.get("name") or "").strip()
            if not name:
                continue
            limit_up = _int(theme.get("limit_up"))
            stage = theme.get("stage")
            sector_text = _sector_text(theme)
            point = {
                "date": date,
                "name": name,
                "count": limit_up,
                "rank": rank,
                "stage": stage,
                "max_board": _int(theme.get("max_board")),
                "news_count": _int(theme.get("news_count")),
                "sector_text": sector_text,
                "group": name,
                "parent_count": limit_up,
            }
            key = (date, name)
            if key not in seen_group:
                seen_group.add(key)
                group_points.append(point)
                bump(group_stats, name, idx, limit_up)
            if date == latest_date:
                latest.append({
                    "name": name,
                    "rank": rank,
                    "limit_up": limit_up,
                    "max_board": point["max_board"],
                    "stage": stage,
                    "news_count": point["news_count"],
                    "sector_text": sector_text,
                })

            for sec in theme.get("sectors") or []:
                sname = str(sec.get("name") or "").strip()
                if not sname:
                    continue
                scount = _int(sec.get("count"))
                skey = (date, sname)
                if skey in seen_sector:
                    continue
                seen_sector.add(skey)
                sector_points.append({
                    "date": date,
                    "name": sname,
                    "count": scount,
                    "rank": rank,
                    "stage": stage,
                    "max_board": point["max_board"],
                    "news_count": point["news_count"],
                    "sector_text": sector_text,
                    "group": name,
                    "parent_count": limit_up,
                })
                bump(sector_stats, sname, idx, scount)

    group_industries = _order_industries(group_stats)
    sector_industries = _order_industries(sector_stats)
    # 点阵纵轴置顶了关注方向，但「持续热度」是独立口径，仍按累计次数取
    hot_industries = _order_industries(group_stats, pin=False)

    group_focus = {n: _focus_label(n) for n in group_industries if _focus_label(n)}
    sector_focus = {n: _focus_label(n) for n in sector_industries if _focus_label(n)}

    ranking = []
    for name in hot_industries[:_TOP_RANKING]:
        s = group_stats[name]
        ranking.append({
            "name": name,
            "count": s["count"],
            "streak": _max_streak(s["indices"]),
            "best": s["best"],
            "last_date": dates[s["last"]] if 0 <= s["last"] < len(dates) else "",
        })

    # 最持续：最长连续上榜 → 总次数 → 最近一次上榜日期（同分的话「现在还在榜上」
    # 比「两周前连过」更像持续热点）；只出现在一天的行业不算「持续」。
    persistent = sorted(
        hot_industries,
        key=lambda n: (
            -_max_streak(group_stats[n]["indices"]),
            -group_stats[n]["count"],
            -group_stats[n]["last"],
            n,
        ),
    )
    most_persistent = (
        persistent[0]
        if persistent and _max_streak(group_stats[persistent[0]]["indices"]) > 1
        else None
    )

    return {
        "dates": dates,
        "focus_groups": [label for label, _ in FOCUS_GROUPS],
        "views": {
            "group": {
                "industries": group_industries,
                "points": group_points,
                "max_count": max((p["count"] for p in group_points), default=0),
                "focus": group_focus,
            },
            "sector": {
                "industries": sector_industries,
                "points": sector_points,
                "max_count": max((p["count"] for p in sector_points), default=0),
                "focus": sector_focus,
            },
        },
        "latest_date": latest_date,
        "latest": latest,
        "ranking": ranking,
        "summary": {
            "report_days": len(dates),
            "industry_count": len(group_industries),
            "sector_count": len(sector_industries),
            "point_count": len(group_points),
            "no_theme_days": no_theme_days,
            "most_persistent": most_persistent,
            "focus_count": len(group_focus),
        },
    }
