/* 股市行情页（/a-share）图表实现：上证指数 + 科创50
 *
 * 由 app/templates/a_share.html 引用；依赖 main.js 的 fetchWithAuth / escapeHtml。
 *
 * 只有一张图：asTrend 双面板走势 —— 两个指数点位差三倍（上证 ~3600 / 科创50 ~1200），
 * 叠一张图会把科创50 压成直线，所以**各自一个 grid 各用各的 Y 轴**，
 * 再用 axisPointer.link 把竖线联动起来（悬停任一栏，两栏指示线对齐）。
 * 为什么不用两张独立 ECharts 实例：各自 autoSkip 算刻度数不同，竖线会错开，
 * 对不齐；单实例多 grid 由库统一算像素，天然对齐（同 /market 页的做法）。
 *
 * 相对强弱不另画归一化曲线（曾有过「年初 = 100」一图，2026-11-02 按用户要求撤掉），
 * 改由卡片里的「年内涨跌幅」承担 —— 两者同为今年以来的累计涨跌，口径已可比。
 *
 * ±阈值摆动（2026-11-10）：上证 ±10% / 科创50 ±20%。
 *   · 卡片：当前值相对整段区间高/低点的偏离达阈值 → 红底（danger），半程 → 琥珀（warn）。
 *   · 曲线：每次跨阈值的「基准高点/低点（▲▼）+ 触发点（●）」，不画基准→触发的连线；
 *     算法同「全球行情」纳斯达克100 的运行峰值/触发重置，只是双向、阈值按指数不同。
 *   数据由后端 /api/a-share 的 item.swing 提供，前端只负责画，不重算口径。
 *
 * 区间按钮也已删除（同日用户要求）：默认就是**全部交易日**，看局部靠 dataZoom ——
 * 底部拖动条 + 滚轮缩放（做法同 /index-alarm）。两个 grid 的 xAxisIndex 一起挂，
 * 缩放时上下两栏始终对齐同一天；拖动条本身只有一条，落在容器最底部。
 */

const AS = {
    ink: "#334155", sub: "#94a3b8", grid: "#e8ebf0", axis: "#cbd5e1",
    up: "#dc2626", down: "#16a34a", sh: "#dc2626", kc: "#2563eb"
};

/* 每个指数的固定配色（与卡片左边框同源，图例与卡片不打架） */
const AS_META = {
    SH000001: { color: AS.sh, area: "rgba(220, 38, 38, 0.07)" },
    SH000688: { color: AS.kc, area: "rgba(37, 99, 235, 0.07)" }
};

const asState = { loading: false };
const asCharts = {};

/* ---------------- 小工具 ---------------- */
function asReady() {
    return typeof echarts !== "undefined" && typeof echarts.init === "function";
}
function asNum(v, d) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    return Number(v).toFixed(d === undefined ? 2 : d);
}
function asSigned(v, d) {
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(d === undefined ? 2 : d);
}
function asTone(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "flat";
    return Number(v) > 0 ? "up" : (Number(v) < 0 ? "down" : "flat");
}
function asHalo(extra) {
    return Object.assign({ textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5 }, extra || {});
}

function asMount(id, option) {
    const dom = document.getElementById(id);
    if (!dom) return;
    if (!asReady()) {
        // 库文件缺失时不能继续调 init（会抛 TypeError，页面整片空白且分不清原因）
        dom.innerHTML = '<div class="chart-load-failed">图表库加载失败，请刷新重试</div>';
        return;
    }
    if (asCharts[id]) { asCharts[id].dispose(); delete asCharts[id]; }
    dom.innerHTML = "";
    const inst = echarts.init(dom);
    inst.setOption(option, true);
    asCharts[id] = inst;
}

function asEmpty(id, msg) {
    const dom = document.getElementById(id);
    if (!dom) return;
    if (asCharts[id]) { asCharts[id].dispose(); delete asCharts[id]; }
    dom.innerHTML = '<div class="as-empty">' + escapeHtml(msg) + "</div>";
}

/* 两条曲线的日期并集（升序）—— 理论上交易日完全一致，仍按并集对齐，缺的点补 null */
function asDates(list) {
    const set = new Set();
    list.forEach(function (ps) { ps.forEach(function (p) { set.add(p.date); }); });
    return Array.from(set).sort();
}

/* ---------------- 卡片 ---------------- */
/* ±阈值摆动说明行：红底由卡片本身承担，这里给口径、参照点与离阈值还差多少 */
function asSwingHtml(it) {
    const sw = it.swing;
    if (!sw || !sw.current) return "";
    const c = sw.current;
    const pct = sw.threshold_pct;
    if (!c.direction) {
        return '<div class="as-swing ok"><span class="as-swing-tag">阈值监测</span>'
            + "<span>区间内点位无偏离，±" + pct + "% 阈值未接近</span></div>";
    }
    const sign = c.direction === "rally" ? "+" : "-";
    const ref = c.ref_date && c.ref_value !== null
        ? c.ref_type + " " + asNum(c.ref_value) + "（" + String(c.ref_date).slice(5) + "）"
        : "—";
    let tag, txt;
    if (c.level === "danger") {
        tag = "阈值触发";
        txt = "当前较" + ref + (c.direction === "rally" ? " 高出 " : " 回撤 ")
            + asSigned(c.deviation_percent) + "%，越过 " + sign + pct + "% 阈值 "
            + asNum(Math.max(0, Math.abs(c.deviation_percent) - pct)) + " 个百分点";
    } else {
        tag = c.level === "warn" ? "接近阈值" : "阈值监测";
        txt = "当前较" + ref + " " + asSigned(c.deviation_percent) + "%，距 "
            + sign + pct + "% 阈值还有 " + asNum(c.remaining_percent) + " 个百分点";
    }
    return '<div class="as-swing ' + c.level + '"><span class="as-swing-tag">' + tag
        + "</span><span>" + escapeHtml(txt) + "</span></div>";
}

function asCardHtml(it) {
    const meta = AS_META[it.symbol] || { color: AS.sub };
    const head = '<div class="as-card-head">'
        + '<span class="as-name">' + escapeHtml(it.name) + "</span>"
        + '<span class="as-code">' + escapeHtml(it.code) + "</span>"
        + '<span class="as-date">' + (it.date ? escapeHtml(it.date) : "暂无数据") + "</span>"
        + "</div>";

    if (it.value === null || it.value === undefined) {
        return '<div class="as-card">' + head
            + '<div class="as-value">--</div>'
            + '<div class="as-metrics">还没有落库数据，点右上「抓取最新」回填今年以来</div></div>';
    }

    const tone = asTone(it.change);
    const changeText = it.change === null
        ? "—"
        : asSigned(it.change) + "（" + asSigned(it.change_percent) + "%）";

    const metrics = []
        .concat(it.ytd_percent === null ? [] : ['年内 <b class="' + asTone(it.ytd_percent)
            + '" style="color:' + (Number(it.ytd_percent) >= 0 ? AS.up : AS.down) + '">'
            + asSigned(it.ytd_percent) + "%</b>"])
        .concat(it.high ? ["年内最高 <b>" + asNum(it.high.value) + "</b>（" + it.high.date + "）"] : [])
        .concat(it.low ? ["年内最低 <b>" + asNum(it.low.value) + "</b>（" + it.low.date + "）"] : [])
        .concat([it.points.length + " 个交易日"]);

    const cur = (it.swing && it.swing.current) || null;
    const level = cur ? cur.level : "none";
    const alertCls = level === "danger" ? " as-alert-danger"
        : (level === "warn" ? " as-alert-warn" : "");
    const borderColor = level === "danger" ? AS.up : meta.color;

    return '<div class="as-card ' + tone + alertCls + '" style="border-left-color:' + borderColor + '">'
        + head
        + '<div class="as-value">' + asNum(it.value) + '<span class="as-unit">' + escapeHtml(it.unit || "") + "</span></div>"
        + '<div class="as-change ' + tone + '">' + changeText + "</div>"
        + '<div class="as-metrics">' + metrics.map(function (m) { return "<span>" + m + "</span>"; }).join("") + "</div>"
        + asSwingHtml(it)
        + "</div>";
}

/* ---------------- 宽基 ETF 净流入 ---------------- */
/* 净流入有正负，颜色按「正红负绿」而不是按涨跌 —— 净申购是买盘（红），净赎回是卖盘（绿）。 */
function asFlowTone(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "flat";
    return Number(v) > 0 ? "up" : (Number(v) < 0 ? "down" : "flat");
}
function asYi(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    return (Number(v) > 0 ? "+" : "") + Number(v).toFixed(2);
}

function asFlowCardHtml(flow) {
    if (!flow) return "";
    const tone = asFlowTone(flow.value);
    const head = '<div class="as-card-head">'
        + '<span class="as-name">' + escapeHtml(flow.name || "宽基ETF净流入") + "</span>"
        + '<span class="as-code">' + (flow.pool ? flow.pool.length : 0) + " 只主力宽基 · 估算口径</span>"
        + '<span class="as-date">' + (flow.date ? escapeHtml(flow.date) : "暂无数据") + "</span>"
        + "</div>";

    if (flow.value === null || flow.value === undefined) {
        return '<div class="as-card as-card-wide flat">' + head
            + '<div class="as-value">--</div>'
            + '<div class="as-metrics">还没有净流入数据，点右上「抓取最新」补齐最近交易日</div></div>';
    }

    const metrics = [
        "近 5 日 <b>" + asYi(flow.sum5) + "</b>",
        "近 20 日 <b>" + asYi(flow.sum20) + "</b>",
        '年内累计 <b style="color:' + (Number(flow.ytd_sum) >= 0 ? AS.up : AS.down) + '">'
            + asYi(flow.ytd_sum) + "</b>",
        "年内最大流入 <b>" + asYi(flow.max_in && flow.max_in.value) + "</b>（"
            + ((flow.max_in && flow.max_in.date) || "—") + "）",
        "年内最大流出 <b>" + asYi(flow.max_out && flow.max_out.value) + "</b>（"
            + ((flow.max_out && flow.max_out.date) || "—") + "）",
        "净流入 " + (flow.inflow_days || 0) + " 天 / 净流出 " + (flow.outflow_days || 0) + " 天"
    ];

    return '<div class="as-card as-card-wide ' + tone + '" style="border-left-color:#7c3aed">'
        + head
        + '<div class="as-value" style="color:' + (Number(flow.value) >= 0 ? AS.up : AS.down) + '">'
        + asYi(flow.value) + '<span class="as-unit">亿元</span></div>'
        + '<div class="as-change ' + tone + '">'
        + (Number(flow.value) >= 0 ? "净申购（资金流入）" : "净赎回（资金流出）") + "</div>"
        + '<div class="as-metrics">' + metrics.map(function (m) { return "<span>" + m + "</span>"; }).join("") + "</div>"
        + "</div>";
}

function asFlowOption(flow) {
    const pts = (flow && flow.points) || [];
    if (!pts.length) return null;
    const dates = pts.map(function (p) { return p.date; });
    const bars = pts.map(function (p) { return p.value; });
    let acc = 0;
    const cum = pts.map(function (p) { acc += p.value; return +acc.toFixed(2); });

    return {
        animationDuration: 260,
        grid: { left: 66, right: 70, top: 40, bottom: 58 },
        legend: {
            top: 0, right: 0, icon: "roundRect", itemWidth: 10, itemHeight: 3,
            textStyle: { color: AS.ink, fontSize: 11 }
        },
        tooltip: {
            trigger: "axis", confine: true,
            axisPointer: { type: "shadow", shadowStyle: { color: "rgba(148,163,184,0.10)" } },
            backgroundColor: "rgba(255,255,255,0.97)",
            borderColor: "#e2e8f0", borderWidth: 1, padding: [8, 11],
            textStyle: { color: AS.ink, fontSize: 12, lineHeight: 18 },
            formatter: function (params) {
                if (!params || !params.length) return "";
                const d = params[0].axisValue;
                const idx = dates.indexOf(d);
                const v = bars[idx];
                if (v === null || v === undefined) return "<b>" + d + "</b>";
                const color = v >= 0 ? AS.up : AS.down;
                return "<b>" + d + "</b><br/>当日净流入：<b style=\"color:" + color + "\">"
                    + asYi(v) + " 亿元</b><br/>年内累计：" + asYi(cum[idx]) + " 亿元";
            }
        },
        xAxis: {
            type: "category", data: dates, boundaryGap: true,
            axisLine: { lineStyle: { color: AS.axis } }, axisTick: { show: false },
            axisLabel: { color: AS.sub, fontSize: 10, hideOverlap: true, showMaxLabel: true },
            splitLine: { show: false }
        },
        // 轴名不写在这里：单位（亿元）已在面板标题里，写上去会和右上角图例打架
        yAxis: [
            {
                type: "value", scale: true,
                axisLine: { show: false }, axisTick: { show: false },
                axisLabel: { color: AS.sub, fontSize: 10 },
                splitLine: { lineStyle: { color: AS.grid } }
            },
            {
                type: "value", scale: true,
                axisLine: { show: false }, axisTick: { show: false },
                axisLabel: { color: AS.sub, fontSize: 10 },
                splitLine: { show: false }
            }
        ],
        dataZoom: [
            { type: "inside", xAxisIndex: 0 },
            {
                type: "slider", xAxisIndex: 0,
                bottom: 4, height: 22, left: 66, right: 70,
                borderColor: "#e2e8f0", backgroundColor: "#f8fafc",
                fillerColor: "rgba(124, 58, 237, 0.10)",
                dataBackground: {
                    lineStyle: { color: "#cbd5e1", width: 1 },
                    areaStyle: { color: "rgba(203, 213, 225, 0.35)" }
                },
                handleStyle: { color: "#fff", borderColor: "#94a3b8", borderWidth: 1 },
                textStyle: { color: AS.sub, fontSize: 10 }
            }
        ],
        series: [
            {
                name: "当日净流入", type: "bar", data: bars,
                barMaxWidth: 14,
                itemStyle: {
                    color: function (p) { return (p.value >= 0 ? AS.up : AS.down); }
                },
                markLine: {
                    silent: true, symbol: "none",
                    lineStyle: { color: AS.axis, width: 1 },
                    label: { show: false },
                    data: [{ yAxis: 0 }]
                }
            },
            {
                name: "年内累计", type: "line", yAxisIndex: 1,
                data: cum, showSymbol: false, symbol: "circle", symbolSize: 6,
                lineStyle: { width: 1.8, color: "#7c3aed" },
                itemStyle: { color: "#7c3aed" }
            }
        ]
    };
}

/* ---------------- 走势图（双面板） ---------------- */
function asTrendOption(items) {
    const slices = items.map(function (it) {
        return { it: it, points: it.points || [] };
    });
    const dates = asDates(slices.map(function (s) { return s.points; }));
    if (!dates.length) return null;

    const valMap = {};
    slices.forEach(function (s) {
        const m = {};
        s.points.forEach(function (p) { m[p.date] = p.value; });
        valMap[s.it.symbol] = m;
    });

    // 高度与 .as-chart-trend 的 CSS 高度一一对应（grid 是固定 px，容器变矮会压掉第二栏）
    // 布局：40 标题 / 232 上证 / 84 间距(含第二栏标题) / 232 科创50 / 26 轴标签 / 拖动条
    const PANEL_H = 232;
    const grids = [
        { left: 64, right: 26, top: 40, height: PANEL_H },
        { left: 64, right: 26, top: 40 + PANEL_H + 84, height: PANEL_H }
    ];

    const titles = [];
    slices.forEach(function (s, i) {
        const tone = asTone(s.it.change_percent);
        const color = tone === "up" ? AS.up : (tone === "down" ? AS.down : AS.sub);
        titles.push({
            text: s.it.name,
            left: grids[i].left, top: grids[i].top - 32,
            textStyle: { fontSize: 13, fontWeight: 600, color: AS.ink }
        });
        titles.push({
            text: s.it.value === null ? "—"
                : asNum(s.it.value) + "  " + asSigned(s.it.change_percent) + "%",
            right: grids[i].right, top: grids[i].top - 32,
            textStyle: { fontSize: 13, fontWeight: 600, color: color, align: "right" }
        });
    });

    const xAxes = grids.map(function (g, i) {
        return {
            type: "category", data: dates, boundaryGap: false,
            gridIndex: i,
            axisLine: { lineStyle: { color: AS.axis } },
            axisTick: { show: false },
            axisLabel: i === grids.length - 1
                ? { color: AS.sub, fontSize: 10, hideOverlap: true, showMaxLabel: true }
                : { show: false },
            splitLine: { show: false }
        };
    });

    const yAxes = grids.map(function (g, i) {
        return {
            type: "value", scale: true, gridIndex: i,
            axisLine: { show: false }, axisTick: { show: false },
            axisLabel: { color: AS.sub, fontSize: 10 },
            splitLine: { lineStyle: { color: AS.grid } }
        };
    });

    const series = slices.map(function (s, i) {
        const style = AS_META[s.it.symbol] || { color: AS.sub, area: "rgba(148,163,184,0.08)" };
        const m = valMap[s.it.symbol] || {};
        return {
            name: s.it.name,
            type: "line",
            xAxisIndex: i, yAxisIndex: i,
            showSymbol: false, symbol: "circle", symbolSize: 6,
            data: dates.map(function (d) { return m[d] === undefined ? null : m[d]; }),
            lineStyle: { width: 1.9, color: style.color },
            itemStyle: { color: style.color },
            areaStyle: { color: style.area },
            connectNulls: false
        };
    });

    // ---- ±阈值摆动标记（算法同纳指100，双向：▲▼ 基准高低点 / ● 跨阈值触发点） ----
    // 新增的 scatter 一律 silent + tooltip:false：它们混进 axis tooltip 的
    // params 会把「上证 / 科创50」的读数串台；formatter 另外按 seriesName 兜底。
    const swingSeries = [];
    slices.forEach(function (s, i) {
        const sw = s.it.swing;
        if (!sw || !sw.events || !sw.events.length) return;
        const evs = sw.events;
        const refData = evs.map(function (e) {
            return {
                value: [e.ref_date, e.ref_value],
                symbol: "triangle",
                symbolRotate: e.type === "drawdown" ? 0 : 180,
                itemStyle: { color: "#94a3b8" },
                label: {
                    show: true, position: e.type === "drawdown" ? "top" : "bottom",
                    formatter: String(e.ref_date).slice(5),
                    color: "#94a3b8", fontSize: 9
                }
            };
        });
        const trigData = evs.map(function (e) {
            return {
                value: [e.trigger_date, e.trigger_value],
                itemStyle: { color: AS.up, borderColor: "#fff", borderWidth: 1 },
                label: {
                    show: true, position: e.type === "drawdown" ? "bottom" : "top",
                    formatter: asSigned(e.change_percent) + "%",
                    color: "#b91c1c", fontSize: 9, fontWeight: 600
                }
            };
        });
        // 只画点、不画连线：基准点到触发日之间留白，避免多档事件叠成一堆横线
        swingSeries.push({
            name: s.it.name + "·基准点", type: "scatter",
            xAxisIndex: i, yAxisIndex: i, symbolSize: 9,
            data: refData, silent: true, tooltip: { show: false }, z: 6
        });
        swingSeries.push({
            name: s.it.name + "·阈值触发", type: "scatter",
            xAxisIndex: i, yAxisIndex: i, symbol: "circle", symbolSize: 8,
            data: trigData, silent: true, tooltip: { show: false }, z: 7
        });
        // 当前值本身已越过阈值：最新点压一个红环。若最新日恰好也是触发日，
        // 上面那个红圆已经标过了，不重复叠。
        const cur = sw.current || {};
        const last = (s.it.points || []).slice(-1)[0];
        if (cur.level === "danger" && last && !cur.triggered) {
            swingSeries.push({
                name: s.it.name + "·当前达标", type: "scatter",
                xAxisIndex: i, yAxisIndex: i, symbol: "circle", symbolSize: 13,
                data: [{
                    value: [last.date, last.value],
                    itemStyle: { color: "rgba(220,38,38,0.16)", borderColor: AS.up, borderWidth: 2 },
                    label: {
                        show: true, position: "top",
                        formatter: asSigned(cur.deviation_percent) + "%",
                        color: "#b91c1c", fontSize: 10, fontWeight: 700
                    }
                }],
                silent: true, tooltip: { show: false }, z: 8
            });
        }
    });

    // ⚠ 只能用**一个** tooltip，不能按 grid 配成数组：tooltip 没有 gridIndex 这个匹配项，
    // 数组写法在鼠标落进第二栏时仍会命中第一个（于是科创50 的悬停显示成了上证的读数）。
    // 多 grid 单 tooltip 时 ECharts 只会带上指针所在那一栏的系列，formatter 再用
    // seriesIndex 回溯到对应指数，双保险。
    const tooltip = {
        trigger: "axis", confine: true,
        axisPointer: { lineStyle: { color: AS.axis } },
        backgroundColor: "rgba(255,255,255,0.97)",
        borderColor: "#e2e8f0", borderWidth: 1, padding: [8, 11],
        textStyle: { color: AS.ink, fontSize: 12, lineHeight: 18 },
        formatter: function (params) {
            if (!params || !params.length) return "";
            const p = params[0];
            // 摆动标记的 scatter 都设了 tooltip:false；万一某个 ECharts 版本仍把它们
            // 塞进 params[0]，按 seriesName 回溯主曲线，避免 seriesIndex 越界串台。
            let s = null;
            for (let k = 0; k < slices.length; k++) {
                if (slices[k].it.name === p.seriesName) { s = slices[k]; break; }
            }
            if (!s) s = slices[p.seriesIndex] || slices[0];
            const m = valMap[s.it.symbol] || {};
            const d = p.axisValue;
            const v = m[d];
            if (v === undefined) {
                return "<b>" + d + "</b><br/>" + escapeHtml(s.it.name) + "：休市";
            }
            const idx = dates.indexOf(d);
            const prev = idx > 0 ? m[dates[idx - 1]] : undefined;
            let extra = "";
            if (prev !== undefined && prev) {
                const chg = v - prev;
                const pct = chg / prev * 100;
                const color = chg >= 0 ? AS.up : AS.down;
                extra = '<br/><span style="color:' + color + '">较前一交易日 '
                    + asSigned(chg) + "（" + asSigned(pct) + "%）</span>";
            }
            return "<b>" + d + "</b><br/>" + escapeHtml(s.it.name)
                + '：<b>' + asNum(v) + "</b>" + extra;
        }
    };

    return {
        animationDuration: 260,
        title: titles,
        grid: grids,
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        // 只缩放不改变数据：拖动条与滚轮都作用于两个 grid，保证上下两栏永远同一天
        dataZoom: [
            {
                type: "inside", xAxisIndex: [0, 1],
                zoomOnMouseWheel: true, moveOnMouseWheel: false,
                preventDefaultMouseMove: true
            },
            {
                type: "slider", xAxisIndex: [0, 1],
                bottom: 4, height: 26, left: 64, right: 26,
                borderColor: "#e2e8f0", backgroundColor: "#f8fafc",
                fillerColor: "rgba(37, 99, 235, 0.12)",
                // 拖动条里那条缩略曲线：不画没人知道两端对应哪段，画了才能一眼定位
                dataBackground: {
                    lineStyle: { color: "#cbd5e1", width: 1 },
                    areaStyle: { color: "rgba(203, 213, 225, 0.35)" }
                },
                selectedDataBackground: {
                    lineStyle: { color: "#93c5fd", width: 1 },
                    areaStyle: { color: "rgba(147, 197, 253, 0.30)" }
                },
                handleStyle: { color: "#fff", borderColor: "#94a3b8", borderWidth: 1 },
                moveHandleStyle: { color: "#94a3b8", opacity: 0.5 },
                textStyle: { color: AS.sub, fontSize: 10 }
            }
        ],
        tooltip: tooltip,
        xAxis: xAxes,
        yAxis: yAxes,
        series: series.concat(swingSeries)
    };
}

/* ---------------- 渲染 ---------------- */
function asRender(data) {
    const items = data.items || [];

    const flow = data.net_flow || null;

    const cards = document.getElementById("asCards");
    cards.innerHTML = items.map(asCardHtml).join("") + asFlowCardHtml(flow);

    // 净流入是金额（亿元、有正负），量纲与点位无关，单独一张图；指数双面板不受影响
    const flowOpt = asFlowOption(flow);
    if (flowOpt) asMount("asFlow", flowOpt); else asEmpty("asFlow", "还没有净流入数据：点右上「抓取最新」补齐");

    const withPoints = items.filter(function (it) { return it.points && it.points.length; });
    if (!withPoints.length) {
        asEmpty("asTrend", "还没有 A 股指数数据：点右上「抓取最新」回填今年以来");
        return;
    }

    const trend = asTrendOption(items);
    if (trend) asMount("asTrend", trend); else asEmpty("asTrend", "所选区间内没有数据");
}

/* 提示条：空库 / 数据滞后 */
function asRenderNotice(data) {
    const box = document.getElementById("asNotice");
    const items = data.items || [];
    const latest = items.map(function (it) { return it.date; })
        .filter(Boolean).sort().pop();
    let html = "";
    if (!data.has_data) {
        html = '<div class="as-notice error"><span class="as-notice-tag">空库</span>'
            + "<div>库里还没有上证指数与科创50 的日线数据。点右上「抓取最新」会回填<strong>今年以来</strong>的全部交易日，"
            + "之后每个交易日 21:34 由定时任务自动补齐。</div></div>";
    } else if (items.some(function (it) { return it.stale_days !== null && it.stale_days > 4; })) {
        const gap = Math.max.apply(null, items.map(function (it) { return it.stale_days || 0; }));
        html = '<div class="as-notice"><span class="as-notice-tag">待更新</span>'
            + "<div>最新落库日期为 <strong>" + escapeHtml(latest || "—") + "</strong>，距今天已 "
            + gap + " 天（周末与节假日属正常）。若已过收盘仍不更新，点「抓取最新」补跑一次。</div></div>";
    }
    box.innerHTML = html;
}

async function asLoad() {
    try {
        const resp = await fetchWithAuth("/api/a-share");
        const data = await resp.json();
        if (data.status !== "ok") {
            document.getElementById("asCards").innerHTML =
                '<div class="error-state"><h2>加载失败</h2><p>' + escapeHtml(data.message || "未知错误") + "</p></div>";
            return;
        }
        const meta = document.getElementById("asUpdated");
        const latest = (data.items || []).map(function (it) { return it.date; })
            .filter(Boolean).sort().pop();
        meta.textContent = "最新交易日 " + (latest || "—") + " · 数据源 " + data.source;
        asRenderNotice(data);
        asRender(data);
    } catch (error) {
        document.getElementById("asCards").innerHTML =
            '<div class="error-state"><h2>加载失败</h2><p>' + escapeHtml(error.message) + "</p></div>";
        asEmpty("asTrend", "加载失败");
        asEmpty("asFlow", "加载失败");
    }
}

/* 手动抓取：同步接口（约 2~6 秒），期间禁用按钮防重复点 */
async function asRefresh() {
    if (asState.loading) return;
    const btn = document.getElementById("asRefresh");
    const old = btn.textContent;
    asState.loading = true;
    btn.disabled = true;
    btn.textContent = "抓取中…";
    try {
        const resp = await fetchWithAuth("/api/a-share/refresh", { method: "POST" });
        const data = await resp.json();
        if (data.status !== "ok") {
            throw new Error(data.message || "抓取失败");
        }
        const added = (data.summary && data.summary.added) || 0;
        btn.textContent = "已写入 " + added + " 行";
        await asLoad();
    } catch (error) {
        btn.textContent = "抓取失败";
        const box = document.getElementById("asNotice");
        box.innerHTML = '<div class="as-notice error"><span class="as-notice-tag">失败</span><div>'
            + escapeHtml(error.message) + "</div></div>";
    } finally {
        asState.loading = false;
        btn.disabled = false;
        setTimeout(function () { btn.textContent = old; }, 2200);
    }
}

document.addEventListener("DOMContentLoaded", function () {
    const btn = document.getElementById("asRefresh");
    if (btn) btn.addEventListener("click", asRefresh);
    asLoad();

    window.addEventListener("resize", debounce(function () {
        Object.keys(asCharts).forEach(function (id) { asCharts[id].resize(); });
    }, 180));
});
