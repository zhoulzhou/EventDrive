/* 股票趋势页图表实现（A 股冷热三层温度计 · 6 张 ECharts）
 * 由 app/templates/stock_temp.html 拆出；当前读数在 stock_temp.html / 股票指标页。
 * 依赖 static/js/main.js 提供的 fetchWithAuth / escapeHtml / debounce。
 * 2026-11-11 用户要求精简：综合温度图只留「综合温度 + 贪婪指数」两条线；
 * 删「破净率」「产业资本净增持」两张图；PE 图删 PB、成交额图删全A换手率（都不再用双轴）。
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
function thresholdLines(pairs) {
    return {
        silent: true, symbol: "none",
        lineStyle: { type: "dashed", width: 1 },
        // 2026-11-11 起本页已无双轴图，阈值文字统一画在绘图区右端外侧
        label: { color: CH.sub, fontSize: 10, formatter: "{b}", position: "end" },
        data: pairs.map(function (p) {
            return { yAxis: p[0], name: p[1], lineStyle: { color: p[2] || CH.slate } };
        })
    };
}
function legend(names) {
    return {
        data: names, top: 2, right: 6, itemWidth: 12, itemHeight: 8,
        textStyle: { color: CH.slate, fontSize: 10.5 }
    };
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
/* 沪深300 PE-TTM：2026-11-11 起按用户要求只画 PE，PB 与右轴一并删掉。 */
function drawPE(bag) {
    const pe = pts(bag.hs300_pe);
    if (!pe.length) return chartEmpty("stPE", "暂无 PE 历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
    const cats = pe.map(function (p) { return p[0]; });
    mount("stPE", {
        grid: baseGrid(), tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("PE-TTM（倍）")],
        series: [
            lineSeries("PE-TTM", pe, CH.blue, { markLine: thresholdLines([
                [20, "PE 20 倍", CH.cyan], [13, "13 倍", CH.slate], [30, "30 倍", CH.orange]
            ]), markPoint: lastMark("PE-TTM", pe, CH.blue, 2) })
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

/* 两市成交额：2026-11-11 起按用户要求只画成交额，全A换手率与右轴一并删掉。
 * 「破净率」整张图也同批下线，历史表里的 below_na 数据保留、本页不再读取。 */
function drawTurnover(bag) {
    const amt = pts(bag.turnover_amt);
    if (!amt.length) return chartEmpty("stTurnover", "暂无成交额历史数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
    const cats = amt.map(function (p) { return p[0]; });
    // 下限压到 5000 亿（或更低），保证 7000 亿地量线也能画出来
    const lo = Math.min(5000, Math.floor(Math.min.apply(null, amt.map(function (p) { return p[1]; })) / 1000) * 1000);
    mount("stTurnover", {
        grid: baseGrid(), tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [Object.assign(yAxis("成交额（亿元）"), { min: lo })],
        series: [
            lineSeries("两市成交额", amt, CH.amber, {
                areaStyle: { color: withAlpha(CH.amber, 0.12) },
                markLine: thresholdLines([
                    [30000, "3 万亿 极端", CH.red], [20000, "2 万亿 亢奋", CH.orange],
                    [12000, "1.2 万亿", CH.lime], [7000, "7000 亿 地量", CH.blue]
                ]),
                markPoint: lastMark("两市成交额", amt, CH.amber, 0, " 亿")
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

/* 资金层 · 新基金发行（月）
 *
 * 原图是「公募仓位 与 新基金发行」双轴。公募仓位指标已于 2026-10-25 下线
 * （股票型基金仓位合同下限 ≥80%，读数常年贴 90%，信息量低），
 * 历史序列不再增长，故本图改为只画新基金发行单轴。 */
function drawFund(bag) {
    const nf = pts(bag.new_fund);
    if (!nf.length) return chartEmpty("stFund", "暂无新基金发行数据，等待定时任务每日收盘写入（可点「刷新数据」立即抓一次）");
    mount("stFund", {
        grid: baseGrid(), tooltip: tipAxis(" 亿"),
        xAxis: Object.assign(xAxis(nf.map(function (p) { return p[0]; })), { boundaryGap: true }),
        yAxis: [yAxis("募集份额（亿元）")],
        series: [{
            name: "新基金发行（月）", type: "bar", data: nf,
            itemStyle: { color: withAlpha(CH.violet, 0.55) },
            barMaxWidth: 16, markPoint: lastMark("新基金发行", nf, CH.violet, 0, " 亿")
        }]
    });
}

/* 「产业资本净增持（滚动30日）」整张图 2026-11-11 下线，
 * 历史表里的 industry_capital 数据保留，本页不再读取、不再渲染容器。 */
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

    // 2026-11-11 用户要求：本图只留「综合温度 + 贪婪指数」两条线，
    // 估值/情绪/资金三层温度不再画（数据仍在 temperature.daily 里，见「股市热度」页）。
    const mkTotal = function () {
        return cats.map(function (c) {
            const r = tempMap[c];
            if (!r || r.total === null || r.total === undefined) return null;
            return r.total;
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
        lineSeries("综合温度", mkTotal(), CH.ink,
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
        legend: legend(["综合温度", "贪婪指数"]),
        tooltip: tipAxis(""),
        xAxis: xAxis(cats),
        yAxis: [yAxis("温度 / 贪婪 0-100")],
        series: series
    });
    const el = document.getElementById("stTempNote");
    if (el) el.textContent = (tp.note || "")
        + "本图只画综合温度与贪婪指数两条线，三层温度明细见「股市热度」页。"
        + "综合温度已积累 " + rows.length + " 个交易日；"
        + "贪婪指数同为 0-100、同为越高越热，与综合温度同轴比高低，"
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
        + fig("综合温度 与 贪婪指数", "每天收盘抓取时打卡一次；两者同为 0-100、越高越热，但口径不同", "stTemp")
        + "<div class=\"st-temp-note\" id=\"stTempNote\"></div>"
        + "</div>"
        + "<div class=\"st-fig-block st-f-valuation\">"
        + "<div class=\"st-fig-block-head\">估值层 · 慢变量定方向</div>"
        + fig("沪深300 PE-TTM", "月频；13 / 20 / 30 倍三条阈值线", "stPE")
        + fig("ERP 股债性价比 = 1/PE − 10Y国债", "月频；样本积累后显示均值 ±1σ 带，越高越便宜", "stERP")
        + "</div>"
        + "<div class=\"st-fig-block st-f-sentiment\">"
        + "<div class=\"st-fig-block-head\">情绪层 · 快变量定情绪</div>"
        + fig("两市成交额", "阈值 7000 / 12000 / 20000 / 30000 亿", "stTurnover")
        + fig("两融占流通市值（%）", "杠杆水位；2.0 / 2.5 / 3.0 / 3.5% 四条阈值线，与指标卡同口径", "stMargin")
        + "</div>"
        + "<div class=\"st-fig-block st-f-capital\">"
        + "<div class=\"st-fig-block-head\">资金层 · 资金变量定底气</div>"
        + fig("新基金发行（权益类，亿元）", "月频；新成立股票型/混合型/指数型基金的募集份额合计", "stFund")
        + "</div></section>";
}

/* ---------------- 走势页说明（本页专用） ---------------- */
function trendNotesHtml() {
    return "<section class=\"st-notes\">"
        + "<h2 class=\"st-group-title\">走势图怎么看</h2>"
        + "<div class=\"st-notes-grid\">"
        + "<div class=\"st-note\"><h3>一、先读总览图</h3>"
        + "<ul>"
        + "<li><b>最上面那张是总览</b>：<b>综合温度</b>（黑线）由估值 / 情绪 / 资金三层温度合成，"
        + "本图只画合成后的总温度与贪婪指数两条线，三层明细见「股市热度」页。</li>"
        + "<li><b>贪婪指数</b>（紫色虚线）与综合温度<b>同轴</b>：两者都是 0-100、都是越高越热，"
        + "可直接比高低。但口径不同——综合温度是「热度分合成」，贪婪指数是「分位合成」，"
        + "不是同一把尺子；它更贴近「贵不贵」，综合温度更贴近「热不热」。</li>"
        + "<li><b>阈值线已内置</b>：沪深300 PE-TTM 13 / 20 / 30 倍、成交额 7000 亿地量 / 1.2 万亿 / 2 万亿亢奋 / 3 万亿极端、"
        + "两融占流通市值 2.0 / 2.5 / 3.0 / 3.5%、ERP 均值 ±1σ。落在线哪一侧，直接读状态。</li>"
        + "<li><b>末端数值标签</b>在每条线最右侧单独一列，是「最新一个数据点」的读数（不是当前快照，含日期差）。</li>"
        + "<li><b>缺数据自动断线</b>——两条线启用时间不同，各自从有数据那天开始画，不做插补。</li>"
        + "<li>红涨绿跌按 A 股惯例：温度计与状态热=红橙、冷=蓝绿。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>二、先看背离，再看绝对水位</h3>"
        + "<ul>"
        + "<li>估值在低位（PE-TTM 低于 13 倍、ERP 高）而<b>资金层抬头</b>（新基金发行放量、两融抬升）= 底部特征。</li>"
        + "<li>情绪层冲高（成交额 2 万亿+）而<b>资金层不动</b>（新基金发行、两融不涨）= 虚火。</li>"
        + "<li>两层打架时对照<b>情绪层与资金层的走势</b>：张口越大，越不该用单一维度下结论。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>三、数据口径</h3>"
        + "<ul>"
        + "<li><b>曲线从现在开始积累</b>：定时任务每天收盘抓一次各指标读数并写入历史表，本页只读库画图，"
        + "<b>不做历史回溯</b>。所以刚启用时曲线很短，越往后越完整。</li>"
        + "<li><b>各指标频率不同</b>：估值层月频（PE-TTM / ERP）、新基金发行月频、成交额 / 两融日频，"
        + "每张图的日期轴按该指标实际有数据的点绘制，不会强行对齐。</li>"
        + "<li><b>缺数据自动断线</b>——某指标当天没取到就断开，不做插补。</li>"
        + "<li>本页只做数据与阈值呈现，<b>不构成任何投资建议</b>。</li>"
        + "</ul></div>"
        + "</div></section>";
}

/* ---------------- 加载与绘制 ---------------- */
function drawCharts(d) {
    const bag = d.series || {};
    // 绘制顺序与页面顺序一致：综合温度在最上（总览），然后估值/情绪/资金分层
    // 贪婪指数与综合温度同图同轴（都是 0-100、越高越热），数据取自 series.greed_index
    drawTemp(d.temperature || {}, bag.greed_index || []);
    drawPE(bag); drawERP(bag); drawTurnover(bag);
    drawMargin(bag); drawFund(bag);
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}

// 6 张图；破净率 / 产业资本净增持整图下线后，容器 id 同步移除
const CHART_IDS = ["stPE", "stERP", "stTurnover", "stMargin", "stFund", "stTemp"];

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
