/* 宏观趋势页图表实现（三组一定位 · 9 张 ECharts）
 * 由 app/templates/macro.html 拆出；当前读数在 macro.html / 宏观指标页。
 * 依赖 static/js/main.js 提供的 fetchWithAuth / escapeHtml / debounce。
 */
/* ==================== 三组一定位 · 宏观监测面板 ==================== */

const CH = {
    ink: "#334155", sub: "#94a3b8", grid: "#e8ebf0", axis: "#cbd5e1", slate: "#64748b",
    blue: "#3b82f6", cyan: "#06b6d4", green: "#22c55e", lime: "#84cc16",
    amber: "#f59e0b", orange: "#f97316", red: "#ef4444"
};
// 图上文字一律加白色描边：标签难免与折线/色带重叠，描边保证始终可读
const HALO = { textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5 };
// 「最新点」用带底色的徽标样式，作为最主要的一处标注
const BADGE = {
    backgroundColor: "rgba(255,255,255,0.9)", borderColor: "#dde3ec", borderWidth: 1,
    borderRadius: 4, padding: [2, 5], textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 1
};
const chartReg = {};
let seriesCache = null;

function withAlpha(hex, a) {
    const h = (hex || "#94a3b8").replace("#", "");
    return "rgba(" + parseInt(h.substring(0, 2), 16) + ", " + parseInt(h.substring(2, 4), 16)
        + ", " + parseInt(h.substring(4, 6), 16) + ", " + a + ")";
}
function pick(bag, key) {
    return ((bag || {})[key] || []).filter(function (p) {
        return p && p.value !== null && p.value !== undefined && !isNaN(Number(p.value));
    });
}
function num(v, d) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    return Number(v).toFixed(d === undefined ? 1 : d);
}
function signed(v, d) {
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(d === undefined ? 1 : d);
}
function movingAvg(values, n) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
        sum += values[i];
        if (i >= n) sum -= values[i - n];
        out.push(i + 1 >= n ? +(sum / n).toFixed(2) : null);
    }
    return out;
}
function anchorRateOn(date, steps) {
    const list = (steps || []).slice().sort(function (a, b) { return a.from < b.from ? 1 : -1; });
    const d = String(date || "").slice(0, 10);
    for (let i = 0; i < list.length; i++) if (d >= list[i].from) return list[i].rate;
    return list.length ? list[list.length - 1].rate : null;
}
function monthlyAvg(rows) {
    const map = {};
    (rows || []).forEach(function (r) {
        const m = String(r.as_of).slice(0, 7);
        (map[m] = map[m] || []).push(Number(r.value));
    });
    return Object.keys(map).sort().map(function (m) {
        const arr = map[m];
        return { as_of: m, value: +(arr.reduce(function (a, b) { return a + b; }, 0) / arr.length).toFixed(2) };
    });
}
function zSeries(rows, win, minObs) {
    win = win || 12; minObs = minObs || 6;
    return (rows || []).map(function (r, i) {
        const w = rows.slice(Math.max(0, i - win + 1), i + 1).map(function (x) { return Number(x.value); });
        if (w.length < minObs) return { as_of: r.as_of, value: null, raw: Number(r.value) };
        const mu = w.reduce(function (a, b) { return a + b; }, 0) / w.length;
        const sd = Math.sqrt(w.reduce(function (a, b) { return a + (b - mu) * (b - mu); }, 0) / w.length);
        return {
            as_of: r.as_of,
            value: sd > 0 ? +((Number(r.value) - mu) / sd).toFixed(2) : 0,
            raw: Number(r.value)
        };
    });
}
// 把某个序列对齐到统一的月份轴，缺失月份补 null
function alignTo(axis, rows, field) {
    const map = {};
    (rows || []).forEach(function (r) { map[r.as_of] = r; });
    return axis.map(function (m) {
        const r = map[m];
        if (!r) return null;
        return field === "raw" ? r.raw : r.value;
    });
}

/* ---------------- 状态词（与后端阈值一致，用于图上直接标注） ---------------- */
function dr007State(v) {
    if (v < -20) return ["松，钱富余", CH.blue];
    if (v < -10) return ["偏松", CH.cyan];
    if (v <= 10) return ["央行合意区间，平稳", CH.green];
    if (v < 20) return ["略偏紧", CH.amber];
    if (v <= 30) return ["边际偏紧", CH.amber];
    if (v < 50) return ["偏紧", CH.orange];
    if (v < 100) return ["明显紧张", CH.orange];
    return ["钱荒级别", CH.red];
}
function r001State(v) {
    if (v < 15) return ["正常", CH.green];
    if (v < 30) return ["分层走阔", CH.amber];
    if (v < 50) return ["结构性紧张", CH.orange];
    return ["极端", CH.red];
}
function tsfState(v) {
    if (v > 12) return ["宽信用 / 过热", CH.red];
    if (v >= 10) return ["偏宽", CH.amber];
    if (v >= 8) return ["中性", CH.green];
    return ["信用收缩", CH.blue];
}
function pmiState(v) {
    if (v >= 52) return ["强劲扩张", CH.red];
    if (v >= 50) return ["温和扩张", CH.amber];
    if (v >= 48) return ["荣枯线附近磨底", CH.green];
    if (v >= 45) return ["明显收缩", CH.cyan];
    return ["深度收缩", CH.blue];
}
function ppiState(v) {
    if (v > 5) return ["过热，成本挤压中下游", CH.red];
    if (v >= 2) return ["偏热、供需两旺", CH.amber];
    if (v >= 0) return ["温和回升", CH.lime];
    if (v >= -3) return ["通缩阴影", CH.cyan];
    return ["深度通缩", CH.blue];
}
function m1m2State(v) {
    if (v > 0) return ["资金活化强", CH.red];
    if (v >= -3) return ["中性", CH.green];
    if (v >= -6) return ["偏冷", CH.cyan];
    return ["深度偏冷", CH.blue];
}

// 短状态词：直接标在「最新点」旁（第 5 个细节：状态词写进图里）
function excessReserveState(v) {
    if (v > 1.5) return ["充裕", CH.blue];
    if (v >= 1.2) return ["中性", CH.green];
    if (v >= 1.0) return ["偏低、资金波动放大", CH.amber];
    return ["偏紧，全靠央行投放吊着", CH.red];
}
function shortDr007(v) {
    if (v < -20) return "松"; if (v < -10) return "偏松"; if (v <= 10) return "平稳";
    if (v < 20) return "略偏紧"; if (v <= 30) return "边际偏紧"; if (v < 50) return "偏紧";
    if (v < 100) return "明显紧张"; return "钱荒";
}
function shortR001(v) {
    if (v < 15) return "正常"; if (v < 30) return "分层走阔";
    if (v < 50) return "结构性紧张"; return "极端";
}
function shortTsf(v) {
    if (v > 12) return "宽信用"; if (v >= 10) return "偏宽"; if (v >= 8) return "中性"; return "信用收缩";
}
function shortPmi(v) {
    if (v >= 52) return "强劲扩张"; if (v >= 50) return "温和扩张";
    if (v >= 48) return "磨底"; if (v >= 45) return "明显收缩"; return "深度收缩";
}
function shortPpi(v) {
    if (v > 5) return "过热"; if (v >= 2) return "偏热"; if (v >= 0) return "温和回升";
    if (v >= -3) return "通缩阴影"; return "深度通缩";
}

/* ---------------- ECharts 通用骨架 ---------------- */
function mount(id, option) {
    const dom = document.getElementById(id);
    if (!dom) return;
    if (chartReg[id]) { chartReg[id].dispose(); delete chartReg[id]; }
    dom.innerHTML = "";
    const inst = echarts.init(dom);
    inst.setOption(option, true);
    chartReg[id] = inst;
}
function chartEmpty(id, msg) {
    const dom = document.getElementById(id);
    if (!dom) return;
    if (chartReg[id]) { chartReg[id].dispose(); delete chartReg[id]; }
    dom.innerHTML = '<div class="mp-empty">' + escapeHtml(msg) + "</div>";
}
function baseOption(dates, o) {
    o = o || {};
    return {
        animationDuration: 260,
        grid: Object.assign({ left: 44, right: 70, top: 30, bottom: 26 }, o.grid || {}),
        tooltip: {
            trigger: "axis", confine: true,
            axisPointer: { lineStyle: { color: CH.axis } },
            backgroundColor: "rgba(255,255,255,0.97)",
            borderColor: "#e2e8f0", borderWidth: 1, padding: [8, 11],
            textStyle: { color: CH.ink, fontSize: 12, lineHeight: 18 },
            formatter: o.tooltip
        },
        xAxis: {
            type: "category", data: dates, boundaryGap: false,
            axisLine: { lineStyle: { color: CH.axis } }, axisTick: { show: false },
            axisLabel: { color: CH.sub, fontSize: 10 },
            splitLine: { show: false }
        },
        yAxis: {
            type: "value", scale: true, min: o.yMin, max: o.yMax,
            name: o.yName, nameGap: 12, nameTextStyle: { color: CH.sub, fontSize: 10 },
            axisLine: { show: false }, axisTick: { show: false },
            axisLabel: { color: CH.sub, fontSize: 10 },
            splitLine: { lineStyle: { color: CH.grid } }
        },
        series: o.series || []
    };
}
// 阈值带（跨越整个 x 轴的横向色带；超出可视范围的部分自动裁掉）
function pushBand(list, x0, x1, y0, y1, yMin, yMax, color, label) {
    const a = Math.max(y0, yMin), b = Math.min(y1, yMax);
    const span = yMax - yMin;
    if (b - a < span * 0.02) return;
    list.push([
        {
            xAxis: x0, yAxis: a,
            itemStyle: { color: withAlpha(color, 0.11) },
            // 标签放到绘图区右侧的留白里，既不压线也不与「最新点」徽标打架
            label: (b - a > span * 0.075 && label) ? Object.assign({
                show: true, position: "right", distance: 6,
                color: withAlpha(color, 0.98), fontSize: 10, fontWeight: "normal", formatter: label
            }, HALO) : { show: false }
        },
        { xAxis: x1, yAxis: b }
    ]);
}
// 极值 + 最新点标注。最新点若正好是极值，则不重复标注极值，并翻转标签方向避免遮挡。
function marksFor(dates, vals, color, latestText, fmt) {
    const mx = Math.max.apply(null, vals), mn = Math.min.apply(null, vals);
    const last = vals[vals.length - 1];
    const eps = Math.abs(mx - mn) * 0.02 + 1e-9;
    const lastIsMax = Math.abs(last - mx) < eps;
    const lastIsMin = Math.abs(last - mn) < eps;
    const data = [];
    if (!lastIsMax) data.push({
        type: "max", name: "高点",
        label: Object.assign({ formatter: fmt("高", mx) }, HALO)
    });
    if (!lastIsMin) data.push({
        type: "min", name: "低点",
        label: Object.assign({ formatter: fmt("低", mn) }, HALO)
    });
    data.push({
        coord: [dates[dates.length - 1], last], value: last, symbolSize: 7,
        itemStyle: { color: color, borderColor: "#fff", borderWidth: 1.5 },
        label: Object.assign({
            show: true, position: lastIsMin ? "top" : (lastIsMax ? "bottom" : "top"),
            distance: 7, offset: [-30, 0],
            color: CH.ink, fontSize: 10, fontWeight: 600, formatter: latestText
        }, BADGE)
    });
    return { symbol: "circle", symbolSize: 5, silent: true, itemStyle: { color: CH.slate }, data: data };
}
function lineSeries(name, vals, color, o) {
    o = o || {};
    // 点数很少时（历史刚开始积累，可能只有 1 个点）必须显示 symbol：
    // ECharts 的 line 系列 symbol:"none" 在单点时不画线也不画点，图表会整片空白，
    // 用户看到的就是「有数据却什么都没有」。
    const sparse = Array.isArray(vals) && vals.filter(function (v) { return v !== null; }).length < 2;
    return Object.assign({
        name: name, type: "line", data: vals,
        smooth: false, symbol: sparse ? "circle" : "none", symbolSize: sparse ? 6 : 0,
        lineStyle: { width: o.width || 1.7, color: color, type: o.dash ? "dashed" : "solid" },
        itemStyle: { color: color }
    }, o.extra || {});
}

/* ---------------- ① 资金松紧：DR007 偏离 ---------------- */
function drawDr007(bag, steps) {
    const rows = pick(bag, "dr007_spread");
    if (!rows.length) { chartEmpty("mpDr007", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const ma5 = movingAvg(vals, 5);
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const pad = Math.max(8, (hi - lo) * 0.18);
    let yMin = Math.min(Math.round((lo - pad) / 5) * 5, -25);
    let yMax = Math.max(Math.round((hi + pad) / 5) * 5, 25);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, -20, yMin, yMax, CH.blue, "松，钱富余");
    pushBand(bands, x0, x1, -20, 20, yMin, yMax, CH.green, "合意区 ±20bp");
    pushBand(bands, x0, x1, 20, 50, yMin, yMax, CH.amber, "偏紧");
    pushBand(bands, x0, x1, 50, yMax, yMin, yMax, CH.red, "钱荒级");

    const opt = baseOption(dates, {
        yName: "bp", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, d = dates[i], v = vals[i];
            const st = dr007State(v), rate = anchorRateOn(d, steps);
            return "<b>" + escapeHtml(d) + "</b><br>"
                + '<span style="color:' + CH.blue + '">●</span> DR007 偏离：<b>' + signed(v, 1) + " bp</b><br>"
                + (rate !== null ? '<span style="color:' + CH.sub + '">锚：7天逆回购 ' + num(rate, 2) + "%</span><br>" : "")
                + (ma5[i] !== null ? '<span style="color:' + CH.amber + '">●</span> 5日均线：' + num(ma5[i], 1) + " bp<br>" : "")
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [
            Object.assign(lineSeries("DR007 偏离", vals, CH.blue), {
                areaStyle: { color: withAlpha(CH.blue, 0.10) },
                markArea: { silent: true, data: bands },
                markLine: {
                    silent: true, symbol: "none",
                    lineStyle: { color: CH.slate, width: 1.2, type: "solid" },
                    label: { position: "insideStartTop", color: CH.slate, textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5, fontSize: 10, formatter: "政策利率 0bp" },
                    data: [{ yAxis: 0 }]
                },
                markPoint: marksFor(
                    dates, vals, CH.blue,
                    shortDr007(vals[vals.length - 1]) + " " + signed(vals[vals.length - 1], 0) + "bp",
                    function (tag, v) { return tag + " " + signed(v, 0) + "bp"; }
                )
            }),
            lineSeries("5 日均线", ma5, CH.amber, { dash: true, width: 1.4 })
        ]
    });
    mount("mpDr007", opt);
}

/* ---------------- ① 资金松紧：R001 − DR001 ---------------- */
function drawR001(bag) {
    const rows = pick(bag, "r001_dr001");
    if (!rows.length) { chartEmpty("mpR001", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const yMin = Math.min(Math.floor((lo - 3) / 5) * 5, 0);
    const yMax = Math.max(Math.ceil((hi + 5) / 5) * 5, 35);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, 15, yMin, yMax, CH.green, "正常 <15bp");
    pushBand(bands, x0, x1, 15, 30, yMin, yMax, CH.amber, "走阔 15~30bp");
    pushBand(bands, x0, x1, 30, 50, yMin, yMax, CH.orange, "紧张 30~50bp");
    pushBand(bands, x0, x1, 50, yMax, yMin, yMax, CH.red, "极端 >50bp");

    const opt = baseOption(dates, {
        yName: "bp", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, v = vals[i], st = r001State(v);
            return "<b>" + escapeHtml(dates[i]) + "</b><br>"
                + '<span style="color:' + CH.cyan + '">●</span> R001 − DR001：<b>' + signed(v, 1) + " bp</b><br>"
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [Object.assign(lineSeries("R001 − DR001", vals, CH.cyan), {
            areaStyle: { color: withAlpha(CH.cyan, 0.10) },
            markArea: { silent: true, data: bands },
            markLine: {
                silent: true, symbol: "none",
                lineStyle: { color: CH.amber, width: 1, type: "dashed", opacity: 0.8 },
                label: { position: "insideStartTop", color: CH.slate, textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5, fontSize: 9 },
                data: [
                    { yAxis: 15, lineStyle: { color: CH.amber }, label: { formatter: "15" } },
                    { yAxis: 30, lineStyle: { color: CH.orange }, label: { formatter: "30" } },
                    { yAxis: 50, lineStyle: { color: CH.red }, label: { formatter: "50" } }
                ]
            },
            markPoint: marksFor(
                dates, vals, CH.cyan,
                shortR001(vals[vals.length - 1]) + " " + signed(vals[vals.length - 1], 0) + "bp",
                function (tag, v) { return tag + " " + signed(v, 0) + "bp"; }
            )
        })]
    });
    mount("mpR001", opt);
}

/* ---------------- ① 资金松紧：超储率（季末·水位） ---------------- */
function drawExcessReserve(bag) {
    const rows = pick(bag, "excess_reserve");
    if (!rows.length) { chartEmpty("mpER", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const yMin = Math.min(Math.floor((lo - 0.15) / 0.1) * 0.1, 1.0);
    const yMax = Math.max(Math.ceil((hi + 0.15) / 0.1) * 0.1, 1.8);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, 1.2, yMin, yMax, CH.amber, "偏低 <1.2%");
    pushBand(bands, x0, x1, 1.2, 1.5, yMin, yMax, CH.green, "中性 1.2~1.5%");
    pushBand(bands, x0, x1, 1.5, yMax, yMin, yMax, CH.blue, "充裕 >1.5%");

    const opt = baseOption(dates, {
        yName: "%", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, v = vals[i], st = excessReserveState(v);
            return "<b>" + escapeHtml(dates[i]) + "</b><br>"
                + '<span style="color:' + CH.green + '">●</span> 季末超额准备金率：<b>' + num(v, 2) + " %</b><br>"
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [Object.assign(lineSeries("超储率", vals, CH.green), {
            areaStyle: { color: withAlpha(CH.green, 0.12) },
            markArea: { silent: true, data: bands },
            markLine: {
                silent: true, symbol: "none",
                lineStyle: { color: CH.amber, width: 1, type: "dashed", opacity: 0.85 },
                label: { position: "insideStartTop", color: CH.slate, textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5, fontSize: 9 },
                data: [
                    { yAxis: 1.2, lineStyle: { color: CH.amber }, label: { formatter: "1.2" } },
                    { yAxis: 1.5, lineStyle: { color: CH.green }, label: { formatter: "1.5" } }
                ]
            },
            markPoint: marksFor(
                dates, vals, CH.green,
                "超储率 " + num(vals[vals.length - 1], 2) + "%",
                function (tag, v) { return tag + " " + num(v, 2) + "%"; }
            )
        })]
    });
    mount("mpER", opt);
}

/* ---------------- ② 信用扩张：社融存量同比 ---------------- */
function drawTsf(bag) {
    const rows = pick(bag, "tsf_yoy");
    if (!rows.length) { chartEmpty("mpTsf", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const yMin = Math.min(Math.floor((lo - 0.5) / 0.5) * 0.5, 6.5);
    const yMax = Math.max(Math.ceil((hi + 0.5) / 0.5) * 0.5, 10.5);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, 8, yMin, yMax, CH.blue, "收缩 <8%");
    pushBand(bands, x0, x1, 8, 10, yMin, yMax, CH.green, "中性 8~10%");
    pushBand(bands, x0, x1, 10, 12, yMin, yMax, CH.amber, "偏宽 10~12%");
    pushBand(bands, x0, x1, 12, yMax, yMin, yMax, CH.red, "宽信用 >12%");

    const opt = baseOption(dates, {
        yName: "%", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, v = vals[i], st = tsfState(v);
            return "<b>" + escapeHtml(dates[i]) + "</b><br>"
                + '<span style="color:' + CH.green + '">●</span> 社融存量同比：<b>' + num(v, 1) + "%</b><br>"
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [Object.assign(lineSeries("社融存量同比", vals, CH.green), {
            areaStyle: { color: withAlpha(CH.green, 0.12) },
            markArea: { silent: true, data: bands },
            markPoint: marksFor(
                dates, vals, CH.green,
                shortTsf(vals[vals.length - 1]) + " " + num(vals[vals.length - 1], 1) + "%",
                function (tag, v) { return tag + " " + num(v, 1) + "%"; }
            )
        })]
    });
    mount("mpTsf", opt);
}

/* ---------------- ② 信用扩张：M1、M2 双线 + 剪刀差填充 ---------------- */
function drawM1M2(bag) {
    const m1 = pick(bag, "m1_yoy"), m2 = pick(bag, "m2_yoy"), sp = pick(bag, "m1_m2");
    // 时间轴取「三条序列里最长的一条」：m1_yoy / m2_yoy 可能尚未单独入库（库里通常只存
    // 剪刀差 m1_m2），原来用 `m1.length >= 2 ? m1 : m2` 判断，m1 为空时会退回同样为空的
    // m2，导致明明有剪刀差数据却画不出图。
    const axisSrc = [m1, m2, sp].filter(function (r) { return r.length; })
        .sort(function (a, b) { return b.length - a.length; })[0] || [];
    const axis = axisSrc.map(function (r) { return r.as_of; });
    if (!axis.length) {
        chartEmpty("mpM1M2", "暂无月度序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）");
        return;
    }

    const a1 = alignTo(axis, m1), a2 = alignTo(axis, m2), asp = alignTo(axis, sp);
    const pairs = axis.map(function (_, i) { return [a1[i], a2[i]]; });
    const both = pairs.filter(function (p) { return p[0] !== null && p[1] !== null; });
    // 成对值（M1 与 M2 同时有）至少要 1 个才画得出剪刀差；只有单边时仍画已有的那条线
    if (!both.length && !asp.filter(function (v) { return v !== null; }).length) {
        chartEmpty("mpM1M2", "M1 / M2 序列不足，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）");
        return;
    }

    // Y 轴范围：优先用成对值（M1/M2 都有）撑开；若只有单边序列，则用该序列 + 剪刀差兜底，
    // 否则 both 为空时 Math.min.apply(null, []) 会得到 Infinity，把整张图压扁。
    const singles = a1.concat(a2).filter(function (v) { return v !== null; })
        .concat(asp.filter(function (v) { return v !== null; }));
    const flat = both.reduce(function (a, p) { return a.concat(p); }, []);
    const pool = flat.length ? flat : singles;
    if (!pool.length) {
        chartEmpty("mpM1M2", "M1 / M2 序列不足，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）");
        return;
    }
    const yMin = Math.floor(Math.min.apply(null, pool) - 1);
    const yMax = Math.ceil(Math.max.apply(null, pool) + 1);

    const spread = asp.map(function (v) { return v === null ? 0 : v; });
    const lastSpread = spread[spread.length - 1];

    // 剪刀差色带：用自定义多边形逐段画「两条线之间」的梯形。
    // （不用 stack 填充，是因为堆叠填充的上下边界不落在两条线上，会画出色带外溢。）
    function bandSeries() {
        return {
            type: "custom", name: "剪刀差带", silent: true, z: 1,
            data: axis.map(function (m) { return [m, 0]; }),
            encode: { x: 0, y: 1 },
            renderItem: function (params, api) {
                const i = params.dataIndex, j = i + 1;
                if (j >= axis.length) return;
                const l0 = a1[i], u0 = a2[i], l1 = a1[j], u1 = a2[j];
                if (l0 === null || u0 === null || l1 === null || u1 === null) return;
                const x0 = api.coord([axis[i], 0])[0];
                const x1 = api.coord([axis[j], 0])[0];
                const y0l = api.coord([axis[i], l0])[1];
                const y0u = api.coord([axis[i], u0])[1];
                const y1l = api.coord([axis[j], l1])[1];
                const y1u = api.coord([axis[j], u1])[1];
                // 配色按「剪刀差」本身的符号（M1 − M2），不是色带的绝对水位
                const s = ((spread[i] || 0) + (spread[j] || 0)) / 2;
                const color = s >= 0 ? CH.green : CH.red;
                return {
                    type: "polygon",
                    shape: { points: [[x0, y0l], [x1, y1l], [x1, y1u], [x0, y0u]] },
                    style: { fill: withAlpha(color, 0.30) },
                    silent: true
                };
            }
        };
    }

    const opt = baseOption(axis, {
        yName: "%", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, raw = asp[i];
            const st = raw === null ? ["—", CH.sub] : m1m2State(raw);
            return "<b>" + escapeHtml(axis[i]) + "</b><br>"
                + '<span style="color:' + CH.amber + '">●</span> M1 同比：<b>' + num(a1[i], 1) + "%</b><br>"
                + '<span style="color:' + CH.blue + '">●</span> M2 同比：<b>' + num(a2[i], 1) + "%</b><br>"
                + '<span style="color:' + st[1] + '">剪刀差（色带）：<b>' + signed(raw === null ? 0 : raw, 1)
                + " pp</b>（" + escapeHtml(st[0]) + "）</span>";
        },
        series: [
            bandSeries(),
            Object.assign(lineSeries("M2 同比", a2, CH.blue, { width: 1.8 }), { connectNulls: false, z: 3 }),
            Object.assign(lineSeries("M1 同比", a1, CH.amber, { width: 1.8 }), {
                connectNulls: false, z: 3,
                markPoint: {
                    symbol: "circle", silent: true, symbolSize: 7,
                    data: [{
                        coord: [axis[axis.length - 1], lastSpread], value: lastSpread,
                        itemStyle: { color: CH.slate, borderColor: "#fff", borderWidth: 1.5 },
                        label: Object.assign({
                            show: true, position: "bottom", distance: 7,
                            color: CH.ink, fontSize: 10, fontWeight: 600,
                            formatter: "剪刀差 " + signed(lastSpread, 1) + "pp"
                        }, BADGE)
                    }]
                }
            })
        ]
    });
    mount("mpM1M2", opt);
}

/* ---------------- ③ 景气温度：PMI ---------------- */
function drawPmi(bag) {
    const rows = pick(bag, "pmi");
    if (!rows.length) { chartEmpty("mpPmi", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const yMin = Math.min(Math.floor(lo) - 1, 45);
    const yMax = Math.max(Math.ceil(hi) + 1, 53);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, 45, yMin, yMax, CH.blue, "深度收缩");
    pushBand(bands, x0, x1, 45, 48, yMin, yMax, CH.cyan, "明显收缩");
    pushBand(bands, x0, x1, 48, 50, yMin, yMax, CH.green, "磨底 48~50");
    pushBand(bands, x0, x1, 50, 52, yMin, yMax, CH.amber, "温和扩张");
    pushBand(bands, x0, x1, 52, yMax, yMin, yMax, CH.red, "强劲扩张");

    const opt = baseOption(dates, {
        yName: "", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, v = vals[i], st = pmiState(v);
            return "<b>" + escapeHtml(dates[i]) + "</b><br>"
                + '<span style="color:' + CH.orange + '">●</span> 制造业 PMI：<b>' + num(v, 1) + "</b><br>"
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [Object.assign(lineSeries("制造业 PMI", vals, CH.orange, { width: 1.9 }), {
            markArea: { silent: true, data: bands },
            markLine: {
                silent: true, symbol: "none",
                lineStyle: { color: CH.slate, width: 1.3 },
                label: { position: "insideStartTop", color: CH.slate, textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5, fontSize: 10, formatter: "荣枯线 50" },
                data: [{ yAxis: 50 }]
            },
            markPoint: marksFor(
                dates, vals, CH.orange,
                shortPmi(vals[vals.length - 1]) + " " + num(vals[vals.length - 1], 1),
                function (tag, v) { return tag + " " + num(v, 1); }
            )
        })]
    });
    mount("mpPmi", opt);
}

/* ---------------- ③ 景气温度：PPI 同比 ---------------- */
function drawPpi(bag) {
    const rows = pick(bag, "ppi_yoy");
    if (!rows.length) { chartEmpty("mpPpi", "暂无历史序列，等待定时任务每日收盘写入（可到「宏观指标」页点「刷新数据」立即抓一次）"); return; }
    const dates = rows.map(function (r) { return r.as_of; });
    const vals = rows.map(function (r) { return Number(r.value); });
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const yMin = Math.min(Math.floor(lo) - 1, -3);
    const yMax = Math.max(Math.ceil(hi) + 1, 6);
    const x0 = dates[0], x1 = dates[dates.length - 1];

    const bands = [];
    pushBand(bands, x0, x1, yMin, -3, yMin, yMax, CH.blue, "深度通缩");
    pushBand(bands, x0, x1, -3, -2, yMin, yMax, CH.cyan, "偏冷");
    pushBand(bands, x0, x1, -2, 0, yMin, yMax, CH.cyan, "通缩阴影");
    pushBand(bands, x0, x1, 0, 2, yMin, yMax, CH.lime, "回升 0~2%");
    pushBand(bands, x0, x1, 2, 5, yMin, yMax, CH.amber, "偏热 2~5%");
    pushBand(bands, x0, x1, 5, yMax, yMin, yMax, CH.red, "过热 >5%");

    const opt = baseOption(dates, {
        yName: "%", yMin: yMin, yMax: yMax,
        tooltip: function (p) {
            const i = p[0].dataIndex, v = vals[i], st = ppiState(v);
            return "<b>" + escapeHtml(dates[i]) + "</b><br>"
                + '<span style="color:' + CH.lime + '">●</span> PPI 同比：<b>' + signed(v, 1) + "%</b><br>"
                + '<span style="color:' + st[1] + '">状态：' + escapeHtml(st[0]) + "</span>";
        },
        series: [Object.assign(lineSeries("PPI 同比", vals, CH.lime, { width: 1.9 }), {
            markArea: { silent: true, data: bands },
            markLine: {
                silent: true, symbol: "none",
                lineStyle: { color: CH.slate, width: 1.1 },
                label: { position: "insideStartTop", color: CH.slate, textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5, fontSize: 10, formatter: "0 轴" },
                data: [{ yAxis: 0 }]
            },
            markPoint: marksFor(
                dates, vals, CH.lime,
                shortPpi(vals[vals.length - 1]) + " " + signed(vals[vals.length - 1], 1) + "%",
                function (tag, v) { return tag + " " + signed(v, 1) + "%"; }
            )
        })]
    });
    mount("mpPpi", opt);
}

/* ---------------- 面板骨架与说明文字 ---------------- */
function panelChart(title, note, id) {
    return '<div class="mp-fig">'
        + '<div class="mp-fig-head"><span class="mp-fig-title">' + escapeHtml(title) + "</span>"
        + (note ? '<span class="mp-fig-note">' + escapeHtml(note) + "</span>" : "") + "</div>"
        + '<div class="mp-chart" id="' + id + '"></div></div>';
}
function panelCard(cls, name, freq, figs, read) {
    return '<div class="mp-card ' + cls + '">'
        + '<div class="mp-card-head"><span class="mp-card-name">' + escapeHtml(name) + "</span>"
        + '<span class="mp-card-freq">' + escapeHtml(freq) + "</span></div>"
        + figs.join("")
        + '<div class="mp-read"><span class="mp-read-label">判读</span>' + escapeHtml(read) + "</div></div>";
}
function panelHtml() {
    return '<section class="mp-wrap">'
        + '<div class="mp-head">'
        + "<h2>宏观监测面板 · 三组一定位</h2>"
        + '<p class="form-hint">日度 / 月度 / 季度分开 · 每个面板 = 主图 + 辅图 + 判读。'
        + "阈值线与色带已内置，落在哪条泳道直接读；当前值、极值都标在图上。<b>曲线从现在开始逐日积累</b>，不再回溯补历史。</p>"
        + "</div>"
        + '<div class="mp-grid">'
        + panelCard("mp-liq", "① 资金松紧", "日度 / 季度", [
            panelChart("主图 · DR007 − 7天逆回购利率", "偏离度线 + 0 轴 + ±20bp 带 + 5 日均线", "mpDr007"),
            panelChart("辅图 · R001 − DR001", "分层利差 + 15 / 30 / 50bp 警戒线", "mpR001"),
            panelChart("水位 · 金融机构超额准备金率（季末）", "季度慢变量 + 1.2 / 1.5% 参考线", "mpER")
        ], "高偏离且分层走阔 = 真紧；只高不阔 = 结构性问题；超储率是资金面的「水位」")
        + panelCard("mp-credit", "② 信用扩张", "月度 · 领先 2-3 季", [
            panelChart("主图 · 社融存量同比", "阈值带：<8 收缩 / 8~10 中性 / >12 宽信用", "mpTsf"),
            panelChart("辅图 · M1、M2 同比 + 剪刀差", "M1>M2 绿、M1<M2 红（面积填充）", "mpM1M2")
        ], "社融回升 + 剪刀差收窄 = 宽信用落地")
        + panelCard("mp-heat", "③ 景气温度", "月度 · 同步", [
            panelChart("主图 · 制造业 PMI", "折线 + 50 荣枯线 + 48/50/52 分档带", "mpPmi"),
            panelChart("辅图 · PPI 同比", "0 轴 + −3/−2/0/2/5% 分档带", "mpPpi")
        ], "PMI 上穿 50 且 PPI 连续回升 = 复苏确认")
        + "</div>"
        + "</section>";
}

function notesHtml() {
    return '<section class="mp-notes">'
        + '<h2 class="mp-notes-title">组合与呈现方法</h2>'
        + '<div class="mp-notes-grid">'
        + '<div class="mp-note">'
        + "<h3>一、组合三原则</h3>"
        + "<ol>"
        + "<li><b>频率对齐</b>：日度（DR007 / R001 / GC001）一张图，月度（社融 / PMI / PPI / M1）一张图，季度（超储率）单独。混频率 = 看不清。</li>"
        + "<li><b>量纲配对</b>：同量纲直接叠（DR007 vs 政策利率）；不同量纲先转成利差 / 偏离度 / 剪刀差再叠。</li>"
        + "<li><b>领先验证</b>：信用（社融、M1−M2）领先实体 2-3 个季度，用于<b>预判</b>；PMI / PPI 是<b>同步确认</b>。两者背离时，以信用为方向、以景气验证。</li>"
        + "</ol></div>"
        + '<div class="mp-note">'
        + "<h3>二、四种呈现形态</h3>"
        + "<ol>"
        + "<li><b>偏离度图</b>（资金面首选）：不裸画两条线让人自己算差，直接画 DR007 − 7天逆回购利率单线 + 0 轴 + ±20bp 色带 + 5 日均线。高于 0 越多越紧、低于 0 越多越松，一眼定生死。</li>"
        + "<li><b>阈值带图</b>（区间类指标首选）：给指标画「泳道」。社融背景分三段色带（&lt;8% 收缩 / 8~10% 中性 / &gt;12% 宽信用）；R001−DR001 画 15 / 30 / 50bp 三条横线；PPI 画 −3 / 0 / 2 / 5% 四档带。泳道比裸线强在<b>阈值内置，不用记</b>。</li>"
        + "<li><b>双线 + 剪刀差填充</b>（配对指标首选）：M1、M2 两条线，中间差值用面积填充，M1&gt;M2 绿色、M1&lt;M2 红色。剪刀差的「转正 / 转负」比两条线的绝对值信息量大得多。</li>"
        + "</ol></div>"
        + '<div class="mp-note">'
        + '<div class="mp-note">'
        + "<h3>三、让图「明显」的细节</h3>"
        + "<ol>"
        + "<li><b>阈值线永远画上</b>：50、±10bp、15/30/50bp、8/10/12%、0 轴——没有锚的图等于没画。</li>"
        + "<li><b>当前值和极值直接标在图上</b>（高峰、低谷、最新点），不靠鼠标悬停。</li>"
        + "<li><b>用填充 / 色带代替裸线</b>：偏离度、剪刀差、分层全部面积化。</li>"
        + "<li><b>日度加 5 日均线去噪；月度一律同比</b>（环比季节性强，会误导）。</li>"
        + "<li><b>状态词写进图里</b>：当前点旁直接标「偏紧 / 平稳 / 偏松」，颜色只是辅助。</li>"
        + "<li><b>一图一问</b>：偏离度图只回答「松紧」，分层图只回答「非银死活」，别把七个指标塞一张图。</li>"
        + "</ol></div>"
        + "</div></section>";
}

/* ---------------- 序列加载与绘制 ---------------- */
function drawCharts(d) {
    const bag = Object.assign({}, d.daily || {}, d.monthly || {});
    const steps = d.anchor_steps || [];
    drawDr007(bag, steps);
    drawR001(bag);
    drawExcessReserve(bag);
    drawTsf(bag);
    drawM1M2(bag);
    drawPmi(bag);
    drawPpi(bag);
    Object.keys(chartReg).forEach(function (k) { chartReg[k].resize(); });
}

async function loadSeries(force) {
    if (seriesCache && !force) { drawCharts(seriesCache); setTrendMeta(seriesCache, false); return; }
    try {
        const response = await fetchWithAuth("/api/macro/series");
        const d = await response.json();
        if (d.status !== "ok") throw new Error(d.message || "获取历史序列失败");
        seriesCache = d;
        drawCharts(d);
        setTrendMeta(d, false);
    } catch (error) {
        ["mpDr007", "mpR001", "mpER", "mpTsf", "mpM1M2", "mpPmi", "mpPpi"].forEach(function (id) {
            chartEmpty(id, "历史序列加载失败：" + error.message);
        });
        setTrendMeta(null, true);
    }
}

/* ==================== 页面装配（宏观趋势） ==================== */

function seriesStats(d) {
    const all = [];
    const collect = function (bag) {
        Object.keys(bag || {}).forEach(function (k) {
            (bag[k] || []).forEach(function (r) { all.push(r); });
        });
    };
    collect(d.daily);
    collect(d.monthly);
    const dates = all.map(function (r) { return String(r.as_of || ""); })
        .filter(function (x) { return x.length >= 7; })
        .sort();
    return { total: all.length, latest: dates.length ? dates[dates.length - 1] : "—" };
}

function nowClock() {
    const n = new Date();
    const p = function (x) { return (x < 10 ? "0" : "") + x; };
    return p(n.getHours()) + ":" + p(n.getMinutes()) + ":" + p(n.getSeconds());
}

// 顶部时间戳 + 样本偏少时的积累提示
function setTrendMeta(d, failed) {
    const meta = document.getElementById("trendUpdated");
    if (!meta) return;
    if (failed) { meta.textContent = "历史序列读取失败"; return; }
    const s = seriesStats(d);
    meta.textContent = "读取于 " + nowClock() + " · 已积累 " + s.total + " 条 · 最新数据 " + s.latest;
    const notice = document.getElementById("trendNotice");
    if (!notice) return;
    notice.innerHTML = s.total === 0
        ? '<div class="macro-alert">历史数据已清空，正从现在开始积累。定时任务每天收盘抓取各指标读数写入历史表，'
          + "曲线会逐日变长；如需立刻写入一批数据，可到「宏观指标」页点「刷新数据」。</div>"
        : (s.total < 30
            ? '<div class="macro-alert">目前已积累 ' + s.total + ' 个数据点，曲线尚短。定时任务每天收盘会自动追加，'
              + "无需手动操作。注意各指标频率不同：日度（DR007 / R001）逐日累积，月度（社融 / M1-M2 / PMI / PPI）每月一点，"
              + "季度（超储率）每季一点，所以月度和季度指标需要等更久才有明显曲线。</div>"
            : "");
}

function trendBoot() {
    const root = document.getElementById("macroTrendRoot");
    if (!root) return;
    // 面板骨架先落 DOM，图表容器到位后再异步取历史序列绘制
    root.innerHTML = panelHtml() + notesHtml();
    loadSeries(false);
}

document.getElementById("macroTrendReload").addEventListener("click", function () { loadSeries(true); });

window.addEventListener("resize", debounce(function () {
    Object.keys(chartReg).forEach(function (k) { chartReg[k].resize(); });
}, 150));

trendBoot();

