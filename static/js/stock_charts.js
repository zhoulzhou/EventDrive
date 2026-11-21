/* 股票趋势页图表实现（A 股冷热三层温度计 · 9 张 ECharts）
 * 由 app/templates/stock_temp.html 拆出；当前读数在 stock_temp.html / 股票指标页。
 * 依赖 static/js/main.js 提供的 fetchWithAuth / escapeHtml / debounce。
 */
const CH = {
    ink: "#334155", sub: "#94a3b8", grid: "#e8ebf0", axis: "#cbd5e1", slate: "#64748b",
    blue: "#3b82f6", cyan: "#06b6d4", green: "#22c55e", lime: "#84cc16",
    amber: "#f59e0b", orange: "#f97316", red: "#ef4444", violet: "#8b5cf6"
};
const HALO = { textBorderColor: "rgba(255,255,255,0.95)", textBorderWidth: 2.5 };
const chartReg = {};
let seriesCache = null;

function withAlpha(hex, a) {
    const h = (hex || "#94a3b8").replace("#", "");
    return "rgba(" + parseInt(h.substring(0, 2), 16) + ", " + parseInt(h.substring(2, 4), 16)
        + ", " + parseInt(h.substring(4, 6), 16) + ", " + a + ")";
}
function pts(rows) {
    return (rows || []).filter(function (p) {
        return p && p.as_of && p.value !== null && p.value !== undefined && !isNaN(Number(p.value));
    }).map(function (p) { return [String(p.as_of).slice(0, 10), Number(p.value)]; });
}
function num(v, d) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    return Number(v).toFixed(d === undefined ? 2 : d);
}
function mount(id, opt) {
    const el = document.getElementById(id);
    if (!el) return;
    chartReg[id] = echarts.init(el);
    chartReg[id].setOption(opt);
}
function chartEmpty(id, msg) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = "<div class=\"st-empty\">" + escapeHtml(msg) + "</div>";
}
function baseGrid() { return { left: 62, right: 92, top: 34, bottom: 34 }; }
function xAxis(cats) {
    return {
        type: "category", data: cats, boundaryGap: false,
        axisLine: { lineStyle: { color: CH.axis } },
        axisLabel: { color: CH.sub, fontSize: 10, hideOverlap: true },
        axisTick: { show: false }
    };
}
function yAxis(name, fmt, position) {
    return {
        type: "value", name: name || "", position: position || "left", scale: true,
        nameTextStyle: { color: CH.sub, fontSize: 10, padding: [0, 0, 4, 0], show: !!name },
        axisLine: { show: false }, axisTick: { show: false },
        splitLine: { lineStyle: { color: CH.grid, type: position === "right" ? "dashed" : "solid" } },
        axisLabel: { color: CH.sub, fontSize: 10, formatter: fmt }
    };
}
function lineSeries(name, data, color, extra) {
    return Object.assign({
        name: name, type: "line", data: data, symbol: "none",
        lineStyle: { width: 1.8, color: color }, itemStyle: { color: color },
        emphasis: { focus: "series" }
    }, extra || {});
}
function lastMark(name, data, color, digits, unit) {
    if (!data.length) return null;
    const last = data[data.length - 1];
    return {
        silent: true,
        data: [{
            name: name, coord: last, value: last[1],
            symbol: "circle", symbolSize: 10,
            itemStyle: { color: color, borderColor: "#fff", borderWidth: 2 },
            label: {
                show: true, position: "right", distance: 6, color: CH.ink,
                fontSize: 10.5, fontWeight: 600, ...HALO,
                formatter: num(last[1], digits === undefined ? 2 : digits) + (unit || "")
            }
        }]
    };
}
function thresholdLines(pairs, inside) {
    return {
        silent: true, symbol: "none",
        lineStyle: { type: "dashed", width: 1 },
        label: {
            color: CH.sub, fontSize: 10, formatter: "{b}",
            // 双轴图上右轴刻度也占右侧泳道，阈值文字改画在绘图区内侧避免压字
            position: inside ? "insideEndTop" : "end"
        },
        data: pairs.map(function (p) {
            return { yAxis: p[0], name: p[1], lineStyle: { color: p[2] || CH.slate } };
        })
    };
}
// hints：图例文字后缀，用来标明该序列挂在哪根轴上（右轴不再重复画轴名）
function legend(names, hints) {
    const opt = {
        data: names, top: 2, right: 6, itemWidth: 12, itemHeight: 8,
        textStyle: { color: CH.slate, fontSize: 10.5 }
    };
    if (hints) {
        opt.formatter = function (name) { return hints[name] ? name + hints[name] : name; };
    }
    return opt;
}
function tipAxis(unit) {
    return {
        trigger: "axis", axisPointer: { type: "cross", lineStyle: { color: CH.axis } },
        backgroundColor: "rgba(255,255,255,0.96)", borderColor: "#e3e8ef", borderWidth: 1,
        textStyle: { color: CH.ink, fontSize: 11 },
        valueFormatter: function (v) { return v === null || v === undefined ? "—" : (Number(v).toFixed(2) + (unit || "")); }
    };
}

/* ---------------- 各图 ---------------- */
function drawPE(bag) {
    const pe = pts(bag.hs300_pe), pb = pts(bag.hs300_pb);
    if (!pe.length) return chartEmpty("stPE", "暂无 PE 历史数据，随每日刷新继续累积");
    const cats = pe.map(function (p) { return p[0]; });
    mount("stPE", {
        grid: baseGrid(),
        legend: legend(["PE-TTM", "PB"], { "PE-TTM": "（倍 · 左轴）", "PB": "（倍 · 右轴）" }),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("PE-TTM（倍）"), yAxis("", null, "right")],
        series: [
            lineSeries("PE-TTM", pe, CH.blue, { markLine: thresholdLines([
                [20, "PE 20 倍", CH.cyan], [13, "13 倍", CH.slate], [30, "30 倍", CH.orange]
            ], true), markPoint: lastMark("PE-TTM", pe, CH.blue, 2) }),
            lineSeries("PB", pb, CH.violet, { yAxisIndex: 1, lineStyle: { width: 1.6, color: CH.violet, type: "dotted" }, markPoint: lastMark("PB", pb, CH.violet, 2) })
        ]
    });
}

function drawERP(bag) {
    const erp = pts(bag.erp);
    if (!erp.length) return chartEmpty("stERP", "暂无 ERP 历史数据，随每日刷新继续累积");
    const vals = erp.map(function (p) { return p[1]; });
    const mu = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
    const sd = Math.sqrt(vals.reduce(function (a, b) { return a + (b - mu) * (b - mu); }, 0) / vals.length);
    mount("stERP", {
        grid: baseGrid(), tooltip: tipAxis("%"),
        xAxis: xAxis(erp.map(function (p) { return p[0]; })),
        yAxis: [yAxis("ERP（%）")],
        series: [lineSeries("ERP", erp, CH.green, {
            areaStyle: { color: withAlpha(CH.green, 0.10) },
            markLine: thresholdLines([
                [mu + sd, "均值+1σ（偏冷）", CH.blue],
                [mu, "十年均值 " + num(mu, 2), CH.slate],
                [mu - sd, "均值−1σ（偏热）", CH.red]
            ]),
            markPoint: lastMark("ERP", erp, CH.green, 2, "%")
        })]
    });
}

function drawBelowNa(bag) {
    const d = pts(bag.below_na);
    if (!d.length) return chartEmpty("stBelowNa", "暂无破净率历史数据，随每日刷新继续累积");
    const tail = d.length > 2600 ? d.slice(-2600) : d;
    mount("stBelowNa", {
        grid: baseGrid(), tooltip: tipAxis("%"),
        xAxis: xAxis(tail.map(function (p) { return p[0]; })),
        yAxis: [yAxis("破净率（%）")],
        series: [lineSeries("破净率", tail, CH.cyan, {
            areaStyle: { color: withAlpha(CH.cyan, 0.10) },
            markLine: thresholdLines([
                [10, "10% 底部区", CH.blue], [6, "6%", CH.cyan], [3, "3%", CH.lime], [1, "1% 无便宜货", CH.red]
            ]),
            markPoint: lastMark("破净率", tail, CH.cyan, 2, "%")
        })]
    });
}

function drawTurnover(bag) {
    const amt = pts(bag.turnover_amt);
    if (!amt.length) return chartEmpty("stTurnover", "暂无成交额历史数据，随每日刷新继续累积");
    const cats = amt.map(function (p) { return p[0]; });
    // 下限压到 5000 亿（或更低），保证 7000 亿地量线也能画出来
    const lo = Math.min(5000, Math.floor(Math.min.apply(null, amt.map(function (p) { return p[1]; })) / 1000) * 1000);
    mount("stTurnover", {
        grid: baseGrid(),
        legend: legend(["两市成交额"]),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: Object.assign(yAxis("成交额（亿元）"), { min: lo }),
        series: [
            lineSeries("两市成交额", amt, CH.amber, {
                areaStyle: { color: withAlpha(CH.amber, 0.12) },
                markLine: thresholdLines([
                    [30000, "3 万亿 极端", CH.red], [20000, "2 万亿 亢奋", CH.orange],
                    [12000, "1.2 万亿", CH.lime], [7000, "7000 亿 地量", CH.blue]
                ], true),
                markPoint: lastMark("两市成交额", amt, CH.amber, 0, " 亿")
            })
        ]
    });
}

function drawMargin(bag) {
    const m = pts(bag.margin).map(function (p) { return [p[0], +(p[1] / 10000).toFixed(4)]; });
    if (!m.length) return chartEmpty("stMargin", "暂无两融历史数据，请点「补齐历史数据」");
    const tail = m.length > 1200 ? m.slice(-1200) : m;
    mount("stMargin", {
        grid: baseGrid(), tooltip: tipAxis(" 万亿"),
        xAxis: xAxis(tail.map(function (p) { return p[0]; })),
        yAxis: [yAxis("两融余额（万亿）")],
        series: [lineSeries("两融余额", tail, CH.red, {
            areaStyle: { color: withAlpha(CH.red, 0.10) },
            markPoint: lastMark("两融余额", tail, CH.red, 4, " 万亿")
        })]
    });
}

function drawFund(bag) {
    const pos = pts(bag.fund_position), nf = pts(bag.new_fund);
    if (!pos.length && !nf.length) return chartEmpty("stFund", "暂无公募仓位 / 新基金发行数据");
    const cats = (pos.length ? pos : nf).map(function (p) { return p[0]; });
    const series = [];
    if (nf.length) {
        series.push({
            name: "新基金发行（月）", type: "bar", yAxisIndex: 1, data: nf,
            itemStyle: { color: withAlpha(CH.violet, 0.55) },
            barMaxWidth: 14, markPoint: lastMark("新基金发行", nf, CH.violet, 0, " 亿")
        });
    }
    if (pos.length) {
        series.push(lineSeries("公募仓位", pos, CH.lime, {
            markLine: thresholdLines([[88, "88% 仓位魔咒", CH.orange], [80, "80%（合同下限附近）", CH.slate]], true),
            markPoint: lastMark("公募仓位", pos, CH.lime, 2, "%")
        }));
    }
    mount("stFund", {
        grid: baseGrid(),
        legend: legend(["公募仓位", "新基金发行（月）"],
            { "公募仓位": "（% · 左轴）", "新基金发行（月）": "（亿元 · 右轴）" }),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("公募仓位（%）"), yAxis("", null, "right")],
        series: series
    });
}

function drawCapital(bag) {
    const c = pts(bag.industry_capital);
    if (!c.length) return chartEmpty("stCapital", "暂无产业资本历史数据，请点「补齐历史数据」");
    mount("stCapital", {
        grid: baseGrid(), tooltip: tipAxis(" 亿"),
        xAxis: Object.assign(xAxis(c.map(function (p) { return p[0]; })), { boundaryGap: true }),
        yAxis: [yAxis("净增持（亿元）")],
        series: [{
            name: "产业资本净增持", type: "bar", data: c.map(function (p) {
                return { value: p[1], itemStyle: { color: p[1] >= 0 ? withAlpha(CH.red, 0.75) : withAlpha(CH.green, 0.75) } };
            }),
            barMaxWidth: 18,
            markLine: thresholdLines([[0, "0 轴（增减持分界）", CH.slate], [100, "增持潮 100 亿", CH.blue], [-400, "减持潮 −400 亿", CH.red]])
        }]
    });
}

function drawStruct(bag) {
    const s = pts(bag.structure_div);
    if (!s.length) return chartEmpty("stStruct", "暂无结构分化序列");
    mount("stStruct", {
        grid: baseGrid(), tooltip: tipAxis(" pp"),
        xAxis: xAxis(s.map(function (p) { return p[0]; })),
        yAxis: [yAxis("分位差（pp）")],
        series: [lineSeries("科创50 − 创业板 分位差", s, CH.violet, {
            areaStyle: { color: withAlpha(CH.violet, 0.10) },
            markLine: thresholdLines([
                [40, "+40pp 极端分化", CH.red], [20, "+20pp", CH.orange],
                [0, "0 轴", CH.slate], [-20, "−20pp", CH.cyan], [-40, "−40pp 极端反向", CH.blue]
            ]),
            markPoint: lastMark("分位差", s, CH.violet, 1, "pp")
        })]
    });
}

function drawTemp(tp) {
    const rows = (tp || {}).monthly || [];
    if (!rows.length) return chartEmpty("stTemp", "历史序列尚未积累，无法重建温度轨迹");
    const cats = rows.map(function (r) { return r.as_of; });
    const mk = function (key) {
        return rows.map(function (r) { return r[key] === null || r[key] === undefined ? null : r[key]; });
    };
    const series = [
        lineSeries("估值温度", mk("valuation"), CH.blue, { connectNulls: false, areaStyle: { color: withAlpha(CH.blue, 0.08) } }),
        lineSeries("情绪温度", mk("sentiment"), CH.amber, { connectNulls: false, symbol: "circle", symbolSize: 4 }),
        lineSeries("资金温度", mk("capital"), CH.green, { connectNulls: false, symbol: "circle", symbolSize: 4 }),
        lineSeries("综合温度", mk("total"), CH.ink, { connectNulls: false, lineStyle: { width: 2.6, color: CH.ink }, symbol: "circle", symbolSize: 5 })
    ];
    mount("stTemp", {
        grid: baseGrid(), legend: legend(["估值温度", "情绪温度", "资金温度", "综合温度"]), tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("温度（0-100）")],
        series: series
    });
    const el = document.getElementById("stTempNote");
    if (el) el.textContent = tp.note || "";
}

/* ---------------- 贪婪指数 与 市场宽度 ---------------- */
const GREED_BANDS = [
    { lo: 0, hi: 10, name: "极端恐慌", color: "#5B8FC7" },
    { lo: 10, hi: 30, name: "恐慌", color: "#8BC8EA" },
    { lo: 30, hi: 70, name: "中性", color: "#E4D48F" },
    { lo: 70, hi: 90, name: "贪婪", color: "#F4B393" },
    { lo: 90, hi: 100, name: "极端贪婪", color: "#EA6668" }
];

function drawGreed(bag) {
    const rows = pts(bag.greed);
    if (rows.length < 2) {
        return chartEmpty("stGreed", "暂无贪婪指数历史（每日收盘后打卡），累积 2 个交易日以上再画");
    }
    const cats = rows.map(function (p) { return p[0]; });
    mount("stGreed", {
        grid: baseGrid(),
        legend: legend(["市场贪婪指数"]),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: Object.assign(yAxis("0-100"), { min: 0, max: 100 }),
        series: [lineSeries("市场贪婪指数", rows, CH.red, {
            markArea: {
                silent: true,
                data: GREED_BANDS.map(function (z) {
                    return [
                        {
                            yAxis: z.lo,
                            itemStyle: { color: withAlpha(z.color, 0.16) },
                            label: {
                                show: true, position: "insideTopLeft", color: CH.slate,
                                fontSize: 10, formatter: z.name
                            }
                        },
                        { yAxis: z.hi }
                    ];
                })
            },
            markLine: thresholdLines([[90, "90 泡沫", CH.red], [70, "70", CH.orange],
                [30, "30", CH.lime], [10, "10", CH.blue]], true),
            markPoint: lastMark("市场贪婪指数", rows, CH.red, 1, "")
        })]
    });
}

function drawBreadth(bag) {
    const rows = pts(bag.breadth);
    if (!rows.length) return chartEmpty("stBreadth", "暂无市场宽度历史，需按日累积");
    mount("stBreadth", {
        grid: baseGrid(),
        legend: legend(["市场宽度"]),
        tooltip: tipAxis("%"),
        xAxis: xAxis(rows.map(function (p) { return p[0]; })),
        yAxis: Object.assign(yAxis("%"), { min: 0, max: 100 }),
        series: [lineSeries("市场宽度", rows, CH.cyan, {
            areaStyle: { color: withAlpha(CH.cyan, 0.12) },
            markLine: thresholdLines([[80, "80% 普涨", CH.red], [50, "50%", CH.slate], [20, "20% 极冷", CH.blue]], true),
            markPoint: lastMark("市场宽度", rows, CH.cyan, 2, "%")
        })]
    });
}

/* ---------------- 面板骨架（顺序即页面顺序） ---------------- */
function fig(title, note, id) {
    return "<div class=\"st-fig\"><div class=\"st-fig-head\"><span class=\"st-fig-title\">" + escapeHtml(title)
        + "</span><span class=\"st-fig-note\">" + escapeHtml(note) + "</span></div>"
        + "<div class=\"st-chart\" id=\"" + id + "\"></div></div>";
}

function panelHtml() {
    return "<section class=\"st-panel\">"
        + "<h2 class=\"st-group-title\">趋势面板<span class=\"st-group-sub\">仅两个指标：市场贪婪指数、两市成交额；曲线自 2026-11-21 起重新累积</span></h2>"
        + "<div class=\"st-fig-block st-f-temp\">"
        + "<div class=\"st-fig-block-head\">贪婪指数 · 合成读数</div>"
        + fig("市场贪婪指数（0-100）", "五档色带：0-10 / 10-30 / 30-70 / 70-90 / 90-100", "stGreed")
        + "</div>"
        + "<div class=\"st-fig-block st-f-sentiment\">"
        + "<div class=\"st-fig-block-head\">情绪层 · 成交额</div>"
        + fig("两市成交额", "成交额阈值 7000 / 12000 / 20000 / 30000 亿", "stTurnover")
        + "</div></section>";
}

/* ---------------- 走势页说明（本页专用） ---------------- */
function trendNotesHtml() {
    return "<section class=\"st-notes\">"
        + "<h2 class=\"st-group-title\">走势图怎么看</h2>"
        + "<div class=\"st-notes-grid\">"
        + "<div class=\"st-note\"><h3>一、本页只看两个指标</h3>"
        + "<ul>"
        + "<li><b>市场贪婪指数</b>：0-100 合成读数，五档色带与股市热度页的状态标签一致。</li>"
        + "<li><b>两市成交额</b>：7000 亿地量 / 1.2 万亿 / 2 万亿 / 3 万亿四条阈值线。</li>"
        + "<li>其余指标（市场宽度、PE/PB、ERP、破净率等）只在<a href=\"/stock-temp\">股市热度</a>页看当前读数。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>二、曲线从 2026-11-21 重新累积</h3>"
        + "<ul>"
        + "<li>更早的历史仍保留在数据库，只是不在本页展示。</li>"
        + "<li>贪婪指数每日收盘后打卡一次，成交额随每日刷新累积，攒够几天曲线才有形状。</li>"
        + "<li>本页只做数据与阈值呈现，<b>不构成任何投资建议</b>。</li>"
        + "</ul></div>"
        + "</div></section>";
}

/* ---------------- 加载与绘制 ---------------- */
function drawCharts(d) {
    const bag = d.series || {};
    drawGreed(bag); drawTurnover(bag);
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}

const CHART_IDS = ["stGreed", "stTurnover"];

async function loadSeries(force) {
    if (seriesCache && !force) { drawCharts(seriesCache); setTrendMeta(seriesCache, false); return; }
    try {
        const response = await fetchWithAuth("/api/stock-temp/series");
        const d = await response.json();
        if (d.status !== "ok") throw new Error(d.message || "获取历史序列失败");
        seriesCache = d;
        drawCharts(d);
        setTrendMeta(d, false);
    } catch (error) {
        CHART_IDS.forEach(function (id) { chartEmpty(id, "历史序列加载失败：" + error.message); });
        setTrendMeta(null, true);
    }
}

/* ==================== 页面装配（股票趋势） ==================== */
function seriesStats(d) {
    const bag = (d || {}).series || {};
    let total = 0, latest = "";
    Object.keys(bag).forEach(function (k) {
        (bag[k] || []).forEach(function (r) {
            total += 1;
            const a = String(r.as_of || "").slice(0, 10);
            if (a.length >= 7 && a > latest) latest = a;
        });
    });
    return { total: total, latest: latest || "—" };
}

function nowClock() {
    const n = new Date();
    const p = function (x) { return (x < 10 ? "0" : "") + x; };
    return p(n.getHours()) + ":" + p(n.getMinutes()) + ":" + p(n.getSeconds());
}

// 顶部时间戳；历史偏少时只做提示，不再提供回溯入口
function setTrendMeta(d, failed) {
    const meta = document.getElementById("stTrendUpdated");
    if (!meta) return;
    if (failed) { meta.textContent = "历史序列读取失败"; return; }
    const s = seriesStats(d);
    meta.textContent = "数据更新于 " + nowClock() + " · 历史 " + s.total + " 条 · 最新数据 " + s.latest;
    const notice = document.getElementById("stTrendNotice");
    if (!notice) return;
    notice.innerHTML = s.total < 300
        ? "<div class=\"st-alert\">历史序列目前只有 " + s.total + " 条，曲线较短。"
          + "本页只展示数据库中已累计的数据，后续随每日刷新继续累积。</div>"
        : "";
}

function trendBoot() {
    const root = document.getElementById("stTrendRoot");
    if (!root) return;
    // 面板骨架先落 DOM，图表容器到位后再异步取历史序列绘制
    root.innerHTML = panelHtml() + trendNotesHtml();
    loadSeries(false);
}

document.getElementById("stTrendReload").addEventListener("click", function () { loadSeries(true); });
window.addEventListener("resize", debounce(function () {
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}, 150));

trendBoot();
