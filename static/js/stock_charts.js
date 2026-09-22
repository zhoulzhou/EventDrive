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
    if (!pe.length) return chartEmpty("stPE", "暂无 PE 历史数据，请点「补齐历史数据」");
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
    if (!erp.length) return chartEmpty("stERP", "暂无 ERP 历史数据，请点「补齐历史数据」");
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
    if (!d.length) return chartEmpty("stBelowNa", "暂无破净率历史数据，请点「补齐历史数据」");
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
    if (!amt.length) return chartEmpty("stTurnover", "暂无成交额历史数据（日频需回填，请点「补齐历史数据」）");
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

/* ---------------- 面板骨架（顺序即页面顺序） ---------------- */
function fig(title, note, id) {
    return "<div class=\"st-fig\"><div class=\"st-fig-head\"><span class=\"st-fig-title\">" + escapeHtml(title)
        + "</span><span class=\"st-fig-note\">" + escapeHtml(note) + "</span></div>"
        + "<div class=\"st-chart\" id=\"" + id + "\"></div></div>";
}

function panelHtml() {
    return "<section class=\"st-panel\">"
        + "<h2 class=\"st-group-title\">趋势面板<span class=\"st-group-sub\">阈值线已内置，落在哪条泳道直接读；缺数据的图层自动断线</span></h2>"
        + "<div class=\"st-fig-block st-f-valuation\">"
        + "<div class=\"st-fig-block-head\">估值层 · 慢变量定方向</div>"
        + fig("沪深300 PE-TTM 与 PB（双轴）", "月频；分位带与当前值标在图上", "stPE")
        + fig("ERP 股债性价比 = 1/PE − 10Y国债", "月频；均值 ±1σ 带，越高越便宜", "stERP")
        + fig("破净率（全部A股）", "日频；10 / 6 / 3 / 1% 四条阈值线", "stBelowNa")
        + "</div>"
        + "<div class=\"st-fig-block st-f-sentiment\">"
        + "<div class=\"st-fig-block-head\">情绪层 · 快变量定情绪</div>"
        + fig("两市成交额 与 全A换手率（双轴）", "成交额阈值 7000 / 12000 / 20000 / 30000 亿", "stTurnover")
        + fig("两融余额（万亿）", "杠杆水位；占流通市值比例见指标卡", "stMargin")
        + "</div>"
        + "<div class=\"st-fig-block st-f-capital\">"
        + "<div class=\"st-fig-block-head\">资金层 · 资金变量定底气</div>"
        + fig("公募仓位 与 新基金发行（双轴）", "股票型基金仓位（周）+ 权益类新成立基金募集（月）", "stFund")
        + fig("产业资本净增持（滚动30日，亿元）", "红柱=净增持 · 绿柱=净减持（A 股涨红跌绿惯例）", "stCapital")
        + "</div>"
        + "<div class=\"st-fig-block st-f-structure\">"
        + "<div class=\"st-fig-block-head\">结构 · 热度在哪个市场</div>"
        + fig("科创50 vs 创业板 · 价格分位差", "月度；±20 / ±40pp 分化阈值线", "stStruct")
        + "</div>"
        + "<div class=\"st-fig-block st-f-temp\">"
        + "<div class=\"st-fig-block-head\">温度轨迹 · 重建口径</div>"
        + fig("三层温度 与 综合温度", "逐点滚动分位；三层齐备才给综合温度", "stTemp")
        + "<div class=\"st-temp-note\" id=\"stTempNote\"></div>"
        + "</div></section>";
}

/* ---------------- 走势页说明（本页专用） ---------------- */
function trendNotesHtml() {
    return "<section class=\"st-notes\">"
        + "<h2 class=\"st-group-title\">走势图怎么看</h2>"
        + "<div class=\"st-notes-grid\">"
        + "<div class=\"st-note\"><h3>一、图里的三层结构</h3>"
        + "<ul>"
        + "<li><b>阈值线已内置</b>：成交额 7000 亿地量 / 2 万亿亢奋 / 3 万亿极端、两融与 ERP 的均值 ±1σ、破净率 10 / 6 / 3 / 1%、"
        + "公募仓位 88%、产业资本 ±100 / −400 亿、结构分化 ±20 / ±40pp。落在线哪一侧，直接读状态。</li>"
        + "<li><b>末端数值标签</b>在每条线最右侧单独一列，是「最新一个数据点」的读数（不是当前快照，含日期差）。</li>"
        + "<li><b>缺数据的图层自动断线</b>——某一层在早期没有数据，曲线就从有数据那天开始画，不做插补。</li>"
        + "<li>红涨绿跌按 A 股惯例：温度计与状态热=红橙、冷=蓝绿；产业资本红柱=净增持、绿柱=净减持。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>二、先看背离，再看绝对水位</h3>"
        + "<ul>"
        + "<li>估值在低位（PE / PB 分位低、ERP 高、破净率高位）而<b>资金层抬头</b>（ETF 份额增、产业资本净增持）= 底部特征。</li>"
        + "<li>情绪层冲高（成交额 2 万亿+、换手率抬升）而<b>资金层不动</b>（公募仓位不增、两融不涨）= 虚火。</li>"
        + "<li>两层打架时用<b>温度轨迹</b>图看分离度：三条线张口越大，越不该用单一维度下结论。</li>"
        + "</ul></div>"
        + "<div class=\"st-note\"><h3>三、数据口径</h3>"
        + "<ul>"
        + "<li><b>分位窗口</b>：月频近 120 个月、周频近 520 周、日频近 2500 个交易日；样本不足 12 期不给分位。</li>"
        + "<li><b>日频历史靠逐日抓取</b>：成交额 / 换手率 / 市场宽度 / ETF份额 的曲线长短取决于回填窗口，"
        + "「补齐历史数据」默认回溯最近 60 个交易日（约需 5 分钟），需要更新或补更长时再点一次（本页与定时任务都不做历史回溯）。</li>"
        + "<li><b>温度轨迹</b>按逐点滚动分位重建，分位样本不足的指标退回按读数档位打分；三层齐备的月份才给综合温度。</li>"
        + "<li><b>结构分化</b>用「价格分位」而非 PE 分位（科创50 上市时间较短，估值序列覆盖不足十年）。</li>"
        + "<li>本页只做数据与阈值呈现，<b>不构成任何投资建议</b>。</li>"
        + "</ul></div>"
        + "</div></section>";
}

/* ---------------- 加载与绘制 ---------------- */
function drawCharts(d) {
    const bag = d.series || {};
    drawPE(bag); drawERP(bag); drawBelowNa(bag); drawTurnover(bag);
    drawMargin(bag); drawFund(bag); drawCapital(bag); drawStruct(bag);
    drawTemp(d.temperature || {});
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}

const CHART_IDS = ["stPE", "stERP", "stBelowNa", "stTurnover", "stMargin", "stFund", "stCapital", "stStruct", "stTemp"];

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

async function runBackfill() {
    const btn = document.getElementById("stBackfill");
    if (!window.confirm(
        "将回溯补齐各指标的历史数据。\n\n"
        + "· 乐咕乐股 / 东财 / 新浪自带历史的指标（PE、PB、ERP、破净率、两融、公募仓位、新基金）几乎瞬时完成；\n"
        + "· 交易所日频数据（成交额、换手率、流通市值）需逐日请求，按最近 60 个交易日补齐，约需 5 分钟；\n"
        + "· 产业资本按月回溯 24 个月。\n\n"
        + "已存在的记录按「指标 + 数据日期」覆盖更新，不会产生重复行。\n继续？")) return;
    btn.disabled = true;
    btn.textContent = "回溯中…（约 5 分钟）";
    try {
        const response = await fetchWithAuth("/api/stock-temp/backfill", { method: "POST" });
        const d = await response.json();
        if (d.status !== "ok") throw new Error(d.message || "回溯失败");
        const lines = Object.keys(d.counts || {}).map(function (k) { return "  " + k + "：" + d.counts[k] + " 条"; });
        alert("回溯完成\n生成 " + d.generated + " 条（新增 " + d.added + " / 更新 " + d.updated + "）\n"
            + lines.join("\n")
            + (Object.keys(d.errors || {}).length ? "\n\n未完成：" + JSON.stringify(d.errors) : ""));
        await loadSeries(true);
    } catch (error) {
        alert("回溯失败：" + error.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "补齐历史数据";
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

// 顶部时间戳 + 历史偏少时的「补齐历史数据」提示
function setTrendMeta(d, failed) {
    const meta = document.getElementById("stTrendUpdated");
    if (!meta) return;
    if (failed) { meta.textContent = "历史序列读取失败"; return; }
    const s = seriesStats(d);
    meta.textContent = "数据更新于 " + nowClock() + " · 历史 " + s.total + " 条 · 最新数据 " + s.latest;
    const notice = document.getElementById("stTrendNotice");
    if (!notice) return;
    notice.innerHTML = s.total < 300
        ? "<div class=\"st-alert\">历史序列偏少（" + s.total + " 条），曲线可能很短。可点「补齐历史数据」回溯："
          + "自带历史的指标瞬时完成，交易所日频数据按最近 60 个交易日逐日请求（约 5 分钟）；"
          + "一律按「指标 + 数据日期」覆盖写入，不会堆重复行。</div>"
        : "";
}

function trendBoot() {
    const root = document.getElementById("stTrendRoot");
    if (!root) return;
    // 面板骨架先落 DOM，图表容器到位后再异步取历史序列绘制
    root.innerHTML = panelHtml() + trendNotesHtml();
    loadSeries(false);
}

document.getElementById("stBackfill").addEventListener("click", runBackfill);
document.getElementById("stTrendReload").addEventListener("click", function () { loadSeries(true); });
window.addEventListener("resize", debounce(function () {
    Object.keys(chartReg).forEach(function (k) { if (chartReg[k]) chartReg[k].resize(); });
}, 150));

trendBoot();
