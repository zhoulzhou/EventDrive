/* ==========================================================================
   主题资金（/theme-capital）渲染 + 图表
   --------------------------------------------------------------------------
   请求：
     GET  /api/theme-capital/snapshot?date=  整份快照（双链明细 + 趋势），带 ETag
     POST /api/theme-capital/refresh          后台点火，立即返回
     GET  /api/theme-capital/refresh-status   轮询刷新进度
   配色：红=涨/净流入，绿=跌/净流出（A 股习惯）。
   ========================================================================== */
(function () {
    "use strict";

    var CHAINS = [
        { key: "compute", name: "算力产业链", color: "#d64545" },
        { key: "robot", name: "人形机器人产业链", color: "#4f7bd9" }
    ];
    var INK = "#334155", SUB = "#8a8f99", GRID = "#eef0f5", AXIS = "#d9dee8";
    var UP = "#d64545", DOWN = "#2f9e6e";

    var state = {
        data: null,        // 当前显示的 {status,date,payload,dates}
        chart: null,
        polling: null
    };

    function $(id) { return document.getElementById(id); }
    function esc(v) { return escapeHtml(String(v === null || v === undefined ? "" : v)); }
    function num(v, digits) {
        if (v === null || v === undefined || isNaN(Number(v))) return "—";
        var d = digits === undefined ? 2 : digits;
        return Number(v).toFixed(d);
    }
    function signed(v, digits) {
        if (v === null || v === undefined || isNaN(Number(v))) return "—";
        var n = Number(v);
        return (n > 0 ? "+" : "") + n.toFixed(digits === undefined ? 2 : digits);
    }
    function cls(v) { return (v > 0 ? "up" : v < 0 ? "down" : ""); }

    // ------------------------------------------------------------ 数据加载
    function load(date) {
        var url = "/api/theme-capital/snapshot" + (date ? "?date=" + encodeURIComponent(date) : "");
        $("tcUpdated").textContent = "加载中…";
        fetchWithAuth(url).then(function (r) { return r.json(); }).then(function (data) {
            state.data = data;
            renderNotice(data);
            if (data.status !== "ok") {
                $("tcUpdated").textContent = "暂无数据";
                $("tcOverview").innerHTML = "";
                $("tcChains").innerHTML = "";
                clearChart();
                return;
            }
            $("tcUpdated").textContent = "数据日期 " + data.date
                + (data.generated_at ? " · 生成于 " + data.generated_at : "");
            renderOverview(data.payload);
            renderTrend(data.payload);
            renderChains(data.payload);
        }).catch(function (err) {
            $("tcUpdated").textContent = "加载失败";
            showNotice("error", "加载失败：" + err.message);
        });
    }

    function clearChart() {
        var dom = $("tcTrendChart");
        if (state.chart) { try { state.chart.dispose(); } catch (e) {} state.chart = null; }
        dom.innerHTML = '<div class="tc-loading">暂无趋势数据</div>';
    }

    function renderNotice(data) {
        var box = $("tcNotice");
        var html = "";
        var gaps = (data.payload || {}).data_gaps || [];
        if (gaps.length) {
            html += '<div class="tc-notice warn">本次抓取有缺口：' + gaps.map(esc).join("；") + "</div>";
        }
        box.innerHTML = html;
    }

    // ------------------------------------------------------------ 概览卡
    function renderOverview(payload) {
        var chains = payload.chains || {};
        $("tcOverview").innerHTML = CHAINS.map(function (meta) {
            var c = chains[meta.key] || {};
            var net = c.net_inflow_yi;
            return '<div class="tc-ov-card ' + meta.key + '">'
                + '<div class="tc-ov-title">' + esc(c.name || meta.name) + "</div>"
                + '<div class="tc-ov-sub">' + esc(c.subtitle || "") + "</div>"
                + '<div class="tc-ov-grid">'
                + ovMetric("全链主力净额", (net === null || net === undefined ? "—" : signed(net, 2) + " 亿"), cls(net))
                + ovMetric("平均涨跌", (c.avg_change_pct === null || c.avg_change_pct === undefined ? "—" : signed(c.avg_change_pct, 2) + "%"), cls(c.avg_change_pct))
                + ovMetric("涨 / 跌", (c.up_count !== undefined ? c.up_count : "—") + " / " + (c.down_count !== undefined ? c.down_count : "—"), "")
                + ovMetric("涨停龙头", (c.leaders || []).length + " 只", "")
                + "</div></div>";
        }).join("");
    }
    function ovMetric(label, value, klass) {
        return '<div class="tc-ov-metric"><div class="tc-ov-label">' + esc(label)
            + '</div><div class="tc-ov-value ' + klass + '">' + value + "</div></div>";
    }

    // ------------------------------------------------------------ 趋势图
    function renderTrend(payload) {
        var dom = $("tcTrendChart");
        var seriesByChain = payload.history || {};
        var allDates = {};
        CHAINS.forEach(function (m) {
            ((seriesByChain[m.key] || [])).forEach(function (p) { if (p.date) allDates[p.date] = 1; });
        });
        var dates = Object.keys(allDates).sort();
        if (!dates.length) { clearChart(); return; }

        if (typeof echarts === "undefined" || typeof echarts.init !== "function") {
            dom.innerHTML = '<div class="chart-load-failed">图表库加载失败，请刷新重试</div>';
            return;
        }
        if (state.chart) { try { state.chart.dispose(); } catch (e) {} }
        state.chart = echarts.init(dom);

        // 两条链量级差异很大（算力动辄上百亿、机器人多为个位数亿）。共用一个纵轴会让
        // 机器人曲线被压成一条贴零的直线，所以各用独立纵轴：compute 走左轴、robot 走右轴。
        // 用折线而非柱形——60 个点画两组柱会互相遮挡，线更能看清趋势与拐点。
        var series = [];
        var yAxes = [];
        CHAINS.forEach(function (m, idx) {
            var map = {};
            ((seriesByChain[m.key] || [])).forEach(function (p) { map[p.date] = p.net_inflow_yi; });
            yAxes.push({
                type: "value",
                name: idx === 0 ? "算力 亿" : "机器人 亿",
                position: idx === 0 ? "left" : "right",
                nameTextStyle: { color: m.color, fontSize: 11 },
                axisLabel: { color: SUB, fontSize: 10 },
                splitLine: { show: idx === 0, lineStyle: { color: GRID } },
                axisLine: { show: false }
            });
            series.push({
                name: m.name,
                type: "line",
                yAxisIndex: idx,
                data: dates.map(function (d) { return map[d] === undefined ? null : map[d]; }),
                showSymbol: false,
                symbol: "circle",
                symbolSize: 6,
                smooth: 0.2,
                connectNulls: true,
                lineStyle: { color: m.color, width: 2 },
                itemStyle: { color: m.color },
                areaStyle: { color: m.color, opacity: 0.06 },
                emphasis: { focus: "series" }
            });
        });

        state.chart.setOption({
            animationDuration: 500,
            grid: { left: 62, right: 62, top: 46, bottom: 62 },
            tooltip: {
                trigger: "axis",
                axisPointer: { type: "line" },
                backgroundColor: "rgba(255,255,255,0.97)", borderColor: AXIS,
                textStyle: { color: INK, fontSize: 12 },
                formatter: function (params) {
                    var s = '<div style="font-weight:600;margin-bottom:4px">' + esc(params[0].axisValue) + "</div>";
                    params.forEach(function (p) {
                        if (p.value === null || p.value === undefined) return;
                        var c = p.value > 0 ? UP : p.value < 0 ? DOWN : INK;
                        var dot = "<span style='display:inline-block;width:8px;height:8px;border-radius:50%;background:" + c + ";margin-right:4px'></span>";
                        s += dot + esc(p.seriesName) + "：<b style='color:" + c + "'>"
                            + signed(p.value, 2) + " 亿</b><br>";
                    });
                    return s;
                }
            },
            legend: { data: CHAINS.map(function (m) { return m.name; }), top: 6, textStyle: { color: SUB } },
            xAxis: {
                type: "category", data: dates, boundaryGap: false,
                axisLine: { lineStyle: { color: AXIS } },
                axisLabel: { color: SUB, fontSize: 10, interval: Math.max(0, Math.floor(dates.length / 12)) },
                axisTick: { show: false }
            },
            yAxis: yAxes,
            dataZoom: dates.length > 24 ? [
                { type: "inside", start: 50, end: 100 },
                { type: "slider", height: 16, bottom: 12, start: 50, end: 100,
                  borderColor: AXIS, fillerColor: "rgba(102,126,234,0.12)",
                  textStyle: { color: SUB, fontSize: 10 } }
            ] : [],
            series: series
        }, true);
    }

    // ------------------------------------------------------------ 链详情
    function renderChains(payload) {
        var chains = payload.chains || {};
        $("tcChains").innerHTML = CHAINS.map(function (m) {
            var c = chains[m.key] || {};
            return '<div class="tc-chain ' + m.key + '">'
                + chainHead(c, m)
                + '<div class="tc-subgrid">'
                    + '<div class="tc-block">' + segBlock(c) + "</div>"
                    + '<div class="tc-block">' + leadersBlock(c) + "</div>"
                + "</div>"
                + '<div class="tc-subgrid" style="margin-top:14px">'
                    + '<div class="tc-block">' + stocksBlock(c) + "</div>"
                    + '<div class="tc-block">' + newsBlock(c) + "</div>"
                + "</div>"
                + "</div>";
        }).join("");
    }

    function chainHead(c, m) {
        var net = c.net_inflow_yi;
        return '<div class="tc-chain-head">'
            + '<div class="tc-chain-title">' + esc(c.name || m.name)
            + '<span class="tc-badge">' + (c.stock_count || 0) + " 只样本</span></div>"
            + '<div class="tc-chain-metrics">'
            + "<span>全链主力净额 <b class='" + cls(net) + "'>" + (net === null || net === undefined ? "—" : signed(net, 2) + " 亿") + "</b></span>"
            + "<span>平均涨跌 <b class='" + cls(c.avg_change_pct) + "'>" + (c.avg_change_pct === null || c.avg_change_pct === undefined ? "—" : signed(c.avg_change_pct, 2) + "%") + "</b></span>"
            + "</div></div>";
    }

    function segBlock(c) {
        var segs = c.by_seg || [];
        if (!segs.length) return '<div class="tc-block-title">细分环节资金流向</div><div class="tc-empty-mini">暂无数据</div>';
        var maxAbs = 1;
        segs.forEach(function (b) { maxAbs = Math.max(maxAbs, Math.abs(b.net_inflow_yi || 0)); });
        var rows = segs.map(function (b) {
            var v = b.net_inflow_yi || 0;
            var w = (Math.abs(v) / maxAbs * 50).toFixed(1);
            var bar = v >= 0
                ? '<div class="tc-seg-bar pos" style="width:' + w + '%"></div>'
                : '<div class="tc-seg-bar neg" style="width:' + w + '%"></div>';
            return '<div class="tc-seg-row">'
                + '<span class="tc-seg-name">' + esc(b.seg) + "</span>"
                + '<span class="tc-seg-bar-wrap">' + bar + '<i class="tc-seg-mid"></i></span>'
                + '<span class="tc-seg-val ' + cls(v) + '">' + signed(v, 2) + "</span>"
                + "</div>";
        }).join("");
        return '<div class="tc-block-title">细分环节资金流向（亿元）</div>' + rows;
    }

    function leadersBlock(c) {
        var leaders = c.leaders || [];
        var html = '<div class="tc-block-title">涨停龙头（篮内）</div>';
        if (!leaders.length) return html + '<div class="tc-empty-mini">当日篮内无个股涨停</div>';
        html += '<div class="tc-leaders">' + leaders.map(function (l) {
            return '<div class="tc-leader">'
                + '<span class="tc-leader-name">' + esc(l.name) + "</span>"
                + '<span class="tc-leader-board">' + (l.board || 1) + " 板</span>"
                + '<div class="tc-leader-meta">' + esc(l.seg)
                + (l.seal_fund_yi !== null && l.seal_fund_yi !== undefined ? " · 封单 " + num(l.seal_fund_yi, 2) + " 亿" : "")
                + (l.broken_times ? " · 炸板 " + l.broken_times + " 次" : "")
                + "</div></div>";
        }).join("") + "</div>";
        return html;
    }

    function stocksBlock(c) {
        var inflow = c.inflow_top || [], outflow = c.outflow_top || [];
        function table(rows, title) {
            var body = rows.map(function (r) {
                return "<tr><td>" + esc(r.name) + '</td><td class="seg-tag">' + esc(r.seg) + "</td>"
                    + '<td class="num ' + cls(r.change_pct) + '">' + (r.change_pct === null || r.change_pct === undefined ? "—" : signed(r.change_pct, 2) + "%") + "</td>"
                    + '<td class="num ' + cls(r.net_inflow_yi) + '">' + (r.net_inflow_yi === null || r.net_inflow_yi === undefined ? "—" : signed(r.net_inflow_yi, 2)) + "</td></tr>";
            }).join("");
            return '<div><div class="tc-block-title">' + title + '</div>'
                + '<table class="tc-table"><thead><tr><th>个股</th><th>环节</th><th class="num">涨跌</th><th class="num">净额(亿)</th></tr></thead>'
                + "<tbody>" + (body || '<tr><td colspan="4" class="tc-empty-mini">暂无数据</td></tr>') + "</tbody></table></div>";
        }
        return '<div class="tc-block-title">链内个股资金榜</div>'
            + '<div class="tc-two-lists">' + table(inflow, "主力净流入前列") + table(outflow, "主力净流出前列") + "</div>";
    }

    function newsBlock(c) {
        var news = c.news || [];
        var html = '<div class="tc-block-title">相关新闻时间轴</div>';
        if (!news.length) return html + '<div class="tc-empty-mini">当日无命中这条链关键词的新闻</div>';
        html += '<ul class="tc-news">' + news.map(function (n) {
            var title = n.url
                ? '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.title) + "</a>"
                : esc(n.title);
            return "<li><span class='tc-news-time'>" + esc(n.time) + "</span>"
                + '<span class="tc-news-tag ' + esc(n.tag) + '">' + esc(n.tag) + "</span>"
                + "<span>" + title + "</span></li>";
        }).join("") + "</ul>";
        return html;
    }

    // ------------------------------------------------------------ 提示条
    function showNotice(kind, msg) {
        $("tcNotice").innerHTML = '<div class="tc-notice ' + kind + '">' + esc(msg) + "</div>";
    }

    // ------------------------------------------------------------ 刷新（后台轮询）
    function startRefresh() {
        $("tcRefresh").disabled = true;
        fetchWithAuth("/api/theme-capital/refresh", { method: "POST" }).then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.status === "running" || data.status === "started") {
                    $("tcRefreshBar").style.display = "flex";
                    pollRefresh();
                } else {
                    $("tcRefresh").disabled = false;
                    showNotice("warn", data.message || "刷新未能启动");
                }
            }).catch(function (err) {
                $("tcRefresh").disabled = false;
                showNotice("error", "刷新请求失败：" + err.message);
            });
    }

    function pollRefresh() {
        if (state.polling) clearTimeout(state.polling);
        state.polling = setTimeout(function () {
            fetchWithAuth("/api/theme-capital/refresh-status").then(function (r) { return r.json(); })
                .then(function (st) {
                    $("tcRefreshMsg").textContent = st.running
                        ? "正在后台抓取，约需 1 分钟…"
                        : "刷新已结束，正在载入新数据…";
                    if (!st.running) {
                        $("tcRefreshBar").style.display = "none";
                        $("tcRefresh").disabled = false;
                        state.polling = null;
                        var res = st.last_result || {};
                        if (st.error || res.status === "error") {
                            showNotice("error", "刷新失败：" + (st.error || "请稍后重试"));
                        } else {
                            showNotice("ok", "刷新完成：" + (res.market_date || "") + "，已载入最新数据");
                        }
                        load(null);
                        return;
                    }
                    pollRefresh();
                }).catch(function () { pollRefresh(); });
        }, 3000);
    }

    // ------------------------------------------------------------ 事件绑定
    document.addEventListener("DOMContentLoaded", function () {
        load(null);
        $("tcRefresh").addEventListener("click", startRefresh);
        window.addEventListener("resize", function () {
            if (state.chart) { try { state.chart.resize(); } catch (e) {} }
        });
    });
})();
