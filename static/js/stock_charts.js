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
/** 图表库（ECharts，本地 /static/js/echarts.min.js）是否可用。 */
function echartsReady() {
    return typeof echarts !== "undefined" && typeof echarts.init === "function";
}

/**
 * ECharts 加载失败时的统一兜底。
 *
 * 库文件虽然已本地化（不再依赖 CDN），仍可能因文件缺失、静态目录挂载异常、
 * 版本不兼容、浏览器拦截等原因没加载成功。此时 `echarts.init` 会直接抛
 * TypeError，页面表现是**整片空白且控制台报错** —— 用户分不清是「没数据」
 * 还是「脚本没跑起来」。这里统一在各图表容器里显示红色提示，避免静默空白。
 */
function echartsLoadFailed(ids) {
    const msg = "图表库加载失败，请刷新重试";
    // 用 chart-load-failed（红色告警）而非 chartEmpty 的常规灰色空态 ——
    // 加载失败是异常，需要与「暂无数据」视觉上区分开
    ids.forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.innerHTML = '<div class="chart-load-failed">' + escapeHtml(msg) + "</div>";
    });
    // 页面级横幅（如有）也提示一次，便于一眼看到原因
    const banner = document.getElementById("stChartAlert");
    if (banner) {
        banner.className = "chart-load-alert";
        banner.style.display = "flex";
        banner.textContent = msg;
    }
}

function mount(id, opt) {
    const el = document.getElementById(id);
    if (!el) return;
    // 兜底：库没加载出来时不要继续调init（会抛 TypeError），改为显示提示
    if (!echartsReady()) { echartsLoadFailed([id]); return; }
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
    if (!pe.length) return chartEmpty("stPE", "暂无 PE 历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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
    if (!erp.length) return chartEmpty("stERP", "暂无 ERP 历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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
    if (!d.length) return chartEmpty("stBelowNa", "暂无破净率历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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
    const amt = pts(bag.turnover_amt), rate = pts(bag.turnover_rate);
    if (!amt.length) return chartEmpty("stTurnover", "暂无成交额历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
    const cats = amt.map(function (p) { return p[0]; });
    // 下限压到 5000 亿（或更低），保证 7000 亿地量线也能画出来
    const lo = Math.min(5000, Math.floor(Math.min.apply(null, amt.map(function (p) { return p[1]; })) / 1000) * 1000);
    mount("stTurnover", {
        grid: baseGrid(),
        legend: legend(["两市成交额", "全A换手率"],
            { "两市成交额": "（亿元 · 左轴）", "全A换手率": "（% · 右轴）" }),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [Object.assign(yAxis("成交额（亿元）"), { min: lo }), yAxis("", null, "right")],
        series: [
            lineSeries("两市成交额", amt, CH.amber, {
                areaStyle: { color: withAlpha(CH.amber, 0.12) },
                markLine: thresholdLines([
                    [30000, "3 万亿 极端", CH.red], [20000, "2 万亿 亢奋", CH.orange],
                    [12000, "1.2 万亿", CH.lime], [7000, "7000 亿 地量", CH.blue]
                ], true),
                markPoint: lastMark("两市成交额", amt, CH.amber, 0, " 亿")
            }),
            lineSeries("全A换手率", rate, CH.red, {
                yAxisIndex: 1, symbol: "circle", symbolSize: 5,
                lineStyle: { width: 1.6, color: CH.red, type: "dashed" },
                markPoint: lastMark("全A换手率", rate, CH.red, 2, "%")
            })
        ]
    });
}

/* 两融占流通市值（%）
 *
 * 历史表 margin 存的是**占流通市值的百分比**（与指标卡「两融占流通市值」同口径、
 * 同判档阈值 2.0/2.5/3.0/3.5%），不是余额。此前这里按余额处理、先除 10000 再画「万亿」，
 * 与指标卡口径不一致且量纲错误（日常刷新写入的是 2.71 这类百分比，被当成亿元除 10000
 * 后画成 0.0003 万亿）。现按百分比直接绘制，并画出与指标卡一致的四条阈值线。 */
function drawMargin(bag) {
    const m = pts(bag.margin);
    if (!m.length) return chartEmpty("stMargin", "暂无两融历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
    const tail = m.length > 1200 ? m.slice(-1200) : m;
    mount("stMargin", {
        grid: baseGrid(), tooltip: tipAxis(" %"),
        xAxis: xAxis(tail.map(function (p) { return p[0]; })),
        yAxis: [yAxis("占流通市值（%）")],
        series: [lineSeries("两融占流通市值", tail, CH.red, {
            areaStyle: { color: withAlpha(CH.red, 0.10) },
            markLine: thresholdLines([[2.0, "低迷2.0"], [2.5, "偏低2.5"], [3.0, "中性3.0"], [3.5, "过热3.5"]]),
            markPoint: lastMark("占流通市值", tail, CH.red, 2, "%")
        })]
    });
}

function drawFund(bag) {
    const pos = pts(bag.fund_position), nf = pts(bag.new_fund);
    if (!pos.length && !nf.length) return chartEmpty("stFund", "暂无公募仓位 / 新基金发行数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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
    if (!c.length) return chartEmpty("stCapital", "暂无产业资本历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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
    if (!s.length) return chartEmpty("stStruct", "暂无结构分化序列，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
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

function drawTemp(tp, greedSeries) {
    const rows = (tp || {}).daily || [];
    const greedPts = pts(greedSeries || []);
    if (!rows.length && !greedPts.length) {
        return chartEmpty("stTemp", "综合温度按日累计，层与层数据齐全后自动出现");
    }

    // 温度打卡(key=temperature) 与 贪婪指数(key=greed_index) 是两个独立历史键，
    // 启用时间不同 → 起始日期可能不一致。这里取**日期并集**做横轴，
    // 各自缺失的那天留 null，由 connectNulls:false 自动断线
    //（与本页「缺数据的图层自动断线、不做插补」的口径一致）。
    const cats = Array.from(new Set(
        rows.map(function (r) { return String(r.as_of).slice(0, 10); })
            .concat(greedPts.map(function (p) { return p[0]; }))
    )).sort();

    const tempMap = {};
    rows.forEach(function (r) { tempMap[String(r.as_of).slice(0, 10)] = r; });
    const greedMap = {};
    greedPts.forEach(function (p) { greedMap[p[0]] = p[1]; });

    const mk = function (key) {
        return cats.map(function (c) {
            const r = tempMap[c];
            if (!r) return null;
            const v = r[key];
            return (v === null || v === undefined) ? null : v;
        });
    };
    const mkGreed = function () {
        return cats.map(function (c) { return greedMap[c] === undefined ? null : greedMap[c]; });
    };

    // 样本积累初期点数很少，用 circle + symbolSize 让每个点都看得见
    const sparse = rows.length < 20;
    const greedSparse = greedPts.length < 20;
    const base = { connectNulls: false, symbol: "circle", symbolSize: 5 };
    const greedBase = { connectNulls: false, symbol: "circle", symbolSize: 6 };

    const series = [
        lineSeries("估值温度", mk("valuation"), CH.blue,
            Object.assign({ areaStyle: { color: withAlpha(CH.blue, 0.08) } }, sparse ? base : {})),
        lineSeries("情绪温度", mk("sentiment"), CH.amber, sparse ? base : { connectNulls: false }),
        lineSeries("资金温度", mk("capital"), CH.green, sparse ? base : { connectNulls: false }),
        lineSeries("综合温度", mk("total"), CH.ink,
            Object.assign({}, sparse ? base : {}, { lineStyle: { width: 2.6, color: CH.ink } })),
        // 贪婪指数：同为 0-100、同为「越高越热」，故与温度同轴可直接比高低；
        // 但它是「分位合成」口径（综合温度是「热度分合成」），不是同一把尺子，
        // 故用虚线区分，避免被误读成同一类指标
        lineSeries("贪婪指数", mkGreed(), CH.violet,
            Object.assign({}, greedSparse ? greedBase : {}, {
                lineStyle: { width: 2, color: CH.violet, type: "dashed" }
            }))
    ];
    mount("stTemp", {
        grid: baseGrid(),
        legend: legend(["估值温度", "情绪温度", "资金温度", "综合温度", "贪婪指数"]),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("温度 / 贪婪 0-100")],
        series: series
    });
    const el = document.getElementById("stTempNote");
    if (el) el.textContent = (tp.note || "")
        + "（当前已积累 " + rows.length + " 个交易日）"
        + "贪婪指数同为 0-100、同为越高越热，与综合温度同轴比高低；"
        + "但它是「分位合成」口径、综合温度是「热度分合成」口径，不是同一把尺子"
        + "（已积累 " + greedPts.length + " 个交易日）。";
}

/* ---------------- 面板骨架（顺序即页面顺序） ---------------- */
function fig(title, note, id) {
    return "<div class=\"st-fig\"><div class=\"st-fig-head\"><span class=\"st-fig-title\">" + escapeHtml(title)
        + "</span><span class=\"st-fig-note\">" + escapeHtml(note) + "</span></div>"
        + "<div class=\"st-chart\" id=\"" + id + "\"></div></div>";
}

function panelHtml() {
    return "<section class=\"st-panel\">"
        + "<h2 class=\"st-group-title\">趋势面板<span class=\"st-group-sub\">阈值线已内置，落在哪条泳道直接读；缺数据的图层自动断线</span></h2>"
        + "<div class=\"st-fig-block st-f-temp\">"
        + "<div class=\"st-fig-block-head\">综合温度 · 每日收盘打卡</div>"
        + fig("三层温度、综合温度 与 贪婪指数", "每天收盘抓取时打卡一次；三层齐备才给综合温度；贪婪指数同为 0-100、越高越热", "stTemp")
        + "<div class=\"st-temp-note\" id=\"stTempNote\"></div>"
        + "</div>"
        + "<div class=\"st-fig-block st-f-valuation\">"
        + "<div class=\"st-fig-block-head\">估值层 · 慢变量定方向</div>"
        + fig("沪深300 PE-TTM 与 PB（双轴）", "月频；图中带十年分位带（样本积累后逐步显现）", "stPE")
        + fig("ERP 股债性价比 = 1/PE − 10Y国债", "月频；样本积累后显示均值 ±1σ 带，越高越便宜", "stERP")
        + fig("破净率（全部A股）", "日频；10 / 6 / 3 / 1% 四条阈值线", "stBelowNa")
        + "</div>"
        + "<div class=\"st-fig-block st-f-sentiment\">"
        + "<div class=\"st-fig-block-head\">情绪层 · 快变量定情绪</div>"
        + fig("两市成交额 与 全A换手率（双轴）", "成交额阈值 7000 / 12000 / 20000 / 30000 亿", "stTurnover")
        + fig("两融占流通市值（%）", "杠杆水位；2.0 / 2.5 / 3.0 / 3.5% 四条阈值线，与指标卡同口径", "stMargin")
        + "</div>"
        + "<div class=\"st-fig-block st-f-capital\">"
        + "<div class=\"st-fig-block-head\">资金层 · 资金变量定底气</div>"
        + fig("公募仓位 与 新基金发行（双轴）", "股票型基金仓位（周）+ 权益类新成立基金募集（月）", "stFund")
        + fig("产业资本净增持（滚动30日，亿元）", "红柱=净增持 · 绿柱=净减持（A 股涨红跌绿惯例）", "stCapital")
        + "</div>"
        + "<div class=\"st-fig-block st-f-structure\">"
        + "<div class=\"st-fig-block-head\">结构 · 热度在哪个市场</div>"
        + fig("科创50 vs 创业板 · 价格分位差", "按月；±20 / ±40pp 分化阈值线", "stStruct")
        + "</div></section>";
}

/* ---------------- 走势页说明（本页专用） ---------------- */
function trendNotesHtml() {
    return "<section class=\"st-notes\">"
        + "<h2 class=\"st-group-title\">走势图怎么看</h2>"
        + "<div class=\"st-notes-grid\">"
        + "<div class=\"st-note\"><h3>一、图里的三层结构</h3>"
        + "<ul>"
        + "<li><b>最上面那张是总览</b>：<b>综合温度</b>（黑线）由估值 / 情绪 / 资金三层温度合成，"
        + "先看它的走势与分档，再往下看是哪一层在拉动。</li>"
        + "<li><b>贪婪指数</b>（紫色虚线）与综合温度<b>同轴</b>：两者都是 0-100、都是越高越热，"
        + "可直接比高低。但口径不同——综合温度是「热度分合成」，贪婪指数是「分位合成」，"
        + "不是同一把尺子；它更贴近「贵不贵」，综合温度更贴近「热不热」。</li>"
        + "<li><b>阈值线已内置</b>：成交额 7000 亿地量 / 2 万亿亢奋 / 3 万亿极端、两融占流通市值 2.0 / 2.5 / 3.0 / 3.5%、ERP 均值 ±1σ、破净率 10 / 6 / 3 / 1%、"
        + "公募仓位 88%、产业资本 ±100 / −400 亿、结构分化 ±20 / ±40pp。落在线哪一侧，直接读状态。</li>"
        + "<li><b>末端数值标签</b>在每条线最右侧单独一列，是「最新一个数据点」的读数（不是当前快照，含日期差）。</li>"
        + "<li><b>缺数据的图层自动断线</b>——某一层在早期没有数据，曲线就从有数据那天开始画，不做插补。</li>"
        + "<li>红涨绿跌按 A 股惯例：温度计与状态热=红橙、冷=蓝绿；产业资本红柱=净增持、绿柱=净减持。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>二、先看背离，再看绝对水位</h3>"
        + "<ul>"
        + "<li>估值在低位（PE / PB 分位低、ERP 高、破净率高位）而<b>资金层抬头</b>（ETF 份额增、产业资本净增持）= 底部特征。</li>"
        + "<li>情绪层冲高（成交额 2 万亿+、换手率抬升）而<b>资金层不动</b>（公募仓位不增、两融不涨）= 虚火。</li>"
        + "<li>两层打架时对照<b>情绪层与资金层的走势</b>：张口越大，越不该用单一维度下结论。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>三、数据口径</h3>"
        + "<ul>"
        + "<li><b>曲线从现在开始积累</b>：定时任务每天收盘抓一次各指标读数并写入历史表，本页只读库画图，"
        + "<b>不做历史回溯</b>。所以刚启用时曲线很短，越往后越完整。</li>"
        + "<li><b>各指标频率不同</b>：估值层月频（PE / ERP）、公募仓位周频、成交额 / 换手率 / 两融 / 破净率日频，"
        + "同一张图里日期轴按各自实际有数据的点绘制，不会强行对齐。</li>"
        + "<li><b>缺数据的图层自动断线</b>——某指标当天没取到就断开，不做插补。</li>"
        + "<li><b>结构分化</b>用「价格分位」而非 PE 分位（科创50 上市时间较短，估值序列覆盖不足十年）。</li>"
        + "<li>本页只做数据与阈值呈现，<b>不构成任何投资建议</b>。</li>"
        + "</ul></div>"
        + "</div></section>";
}

/* ---------------- 加载与绘制 ---------------- */
function drawCharts(d) {
    const bag = d.series || {};
    // 绘制顺序与页面顺序一致：综合温度在最上（总览），然后估值/情绪/资金/结构分层
    // 贪婪指数与综合温度同图同轴（都是 0-100、越高越热），数据取自 series.greed_index
    drawTemp(d.temperature || {}, bag.greed_index || []);
    drawPE(bag); drawERP(bag); drawBelowNa(bag); drawTurnover(bag);
    drawMargin(bag); drawFund(bag); drawCapital(bag); drawStruct(bag);
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}

const CHART_IDS = ["stPE", "stERP", "stBelowNa", "stTurnover", "stMargin", "stFund", "stCapital", "stStruct", "stTemp"];

async function loadSeries(force) {
    // 前置兜底：库没加载出来就没必要再请求数据了，直接给提示（避免白跑一趟请求）
    if (!echartsReady()) { echartsLoadFailed(CHART_IDS); return; }
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

// 顶部时间戳 + 样本偏少时的积累提示
function setTrendMeta(d, failed) {
    const meta = document.getElementById("stTrendUpdated");
    if (!meta) return;
    if (failed) { meta.textContent = "历史序列读取失败"; return; }
    const s = seriesStats(d);
    meta.textContent = "读取于 " + nowClock() + " · 已积累 " + s.total + " 条 · 最新数据 " + s.latest;
    const notice = document.getElementById("stTrendNotice");
    if (!notice) return;
    const days = ((d || {}).temperature || {}).daily || [];
    notice.innerHTML = s.total === 0
        ? "<div class=\"st-alert\">历史数据已清空，正从现在开始积累。定时任务每天收盘抓取各指标读数写入历史表，"
          + "曲线会逐日变长；如需立刻写入一批数据，可到「股市热度」页点「刷新数据」。</div>"
        : (s.total < 60
            ? "<div class=\"st-alert\">目前已积累 " + s.total + " 个数据点、每日温度打卡 " + days.length
              + " 天，曲线尚短。定时任务每天收盘会自动追加，无需手动操作；样本积累后分位带等统计口径才会显现。</div>"
            : "");
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
