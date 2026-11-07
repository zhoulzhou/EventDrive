/* ==========================================================================
   热点趋势（/hotspot-trend）图表实现
   --------------------------------------------------------------------------
   只有一次请求：GET /api/hotspot/trend?days=60。页面不再提供「区间 / 口径」
   选择器（同 /a-share 的做法）：默认看**全部可用交易日**（后端上限 60，约一季度），
   看局部靠 dataZoom；口径固定为主线方向（views.group）。后端仍会一并返回
   views.sector，但本页不展示，接口保持兼容。

   图是 ECharts scatter：
     x = 交易日（category，只列有日报的日期）
     y = 热点行业（category；半导体/计算机/人形机器人相关固定在顶部，其余按
         累计上榜次数降序，inverse 让第一行在顶部）
     一个圆点 = 某天该行业进入今日主线
     圆点大小 = 涨停家数；圆点颜色与纵轴标签颜色 = 所在行位置（顶部红 → 底部蓝）
     当日主线排名仍在 tooltip 里；最新交易日描白边
   ========================================================================== */
(function () {
    "use strict";

    var HT = {
        ink: "#334155",
        sub: "#8a8f99",
        grid: "#eef0f5",
        axis: "#d9dee8"
    };

    // 固定展示参数：页面已删掉「区间」「口径」两个下拉，改这里即可调整。
    // 60 = 后端 list_hotspot_reports 的上限，等于「不截断地看全部历史」。
    var FIXED_DAYS = 60;
    var FIXED_MODE = "group";
    var MODE_LABEL = "主线方向";

    var state = {
        data: null,
        busy: false,
        chart: null
    };

    function $(id) { return document.getElementById(id); }

    function esc(value) {
        return escapeHtml(String(value === null || value === undefined ? "" : value));
    }

    function num(value) {
        if (value === null || value === undefined || isNaN(Number(value))) return "—";
        return String(Number(value));
    }

    /* 纵轴行色：顶部红 → 底部蓝。刻意不经过黄绿，免得和「涨红跌绿」的涨跌
       语义撞车。色相分段映射：红系占前 40% 的行、蓝系占后 40%，中间 20% 用紫
       过渡 —— 若把 0°→220° 直接线性铺开，12 行里会有 8 行挤在品红/紫区间，
       看起来是「整片紫」而不是渐变。饱和度与亮度固定，浅色底上每行都可读。
       t = 行号 / (行数-1)。 */
    function gradientColor(t) {
        var p = Math.min(1, Math.max(0, Number(t) || 0));
        var hue = p < 0.4 ? (360 - 45 * (p / 0.4)) % 360
            : p < 0.6 ? 315 - 60 * ((p - 0.4) / 0.2)
                : 255 - 45 * ((p - 0.6) / 0.4);
        var s = 0.68, l = 0.45;
        var c = (1 - Math.abs(2 * l - 1)) * s;
        var x = c * (1 - Math.abs((hue / 60) % 2 - 1));
        var m = l - c / 2;
        var r = 0, g = 0, b = 0;
        if (hue < 60) { r = c; g = x; }
        else if (hue < 120) { r = x; g = c; }
        else if (hue < 180) { g = c; b = x; }
        else if (hue < 240) { g = x; b = c; }
        else if (hue < 300) { r = x; b = c; }
        else { r = c; b = x; }
        return "#" + [r, g, b].map(function (v) {
            var h = Math.round((v + m) * 255).toString(16);
            return h.length < 2 ? "0" + h : h;
        }).join("");
    }

    function withAlpha(hex, alpha) {
        var v = parseInt(hex.slice(1), 16);
        return "rgba(" + ((v >> 16) & 255) + "," + ((v >> 8) & 255) + ","
            + (v & 255) + "," + alpha + ")";
    }

    /* 圆点直径：按涨停家数开方映射，小点也看得见，大点不至于糊成一团 */
    function dotSize(count, maxCount) {
        var ratio = Math.sqrt((Number(count) || 0) / Math.max(1, Number(maxCount) || 1));
        return Math.max(9, Math.min(25, 9 + 16 * ratio));
    }

    function notice(kind, text) {
        var box = $("htNotice");
        if (!box) return;
        box.innerHTML = text
            ? '<div class="ht-notice ' + kind + '">' + esc(text) + "</div>"
            : "";
    }

    /* ---------------------------------------------------------------- 统计条 */
    function renderStats(data, view) {
        var s = data.summary || {};
        // 行业数与圆点数取当前实际绘制的那份点阵，避免与图上行数对不上
        var industries = view && view.industries ? view.industries.length : s.industry_count;
        var dots = view && view.points ? view.points.length : s.point_count;
        var cards = [
            ["有日报交易日", s.report_days, "天", "横轴上的日期数"],
            ["出现过的行业", industries, "个", "纵轴行业数"],
            ["累计圆点", dots, "个", "一次上榜 = 一个圆点"],
            ["无主线交易日", s.no_theme_days, "天", "有日报但没凑够主线"]
        ];
        $("htStats").innerHTML = cards.map(function (c) {
            return '<div class="ht-stat">'
                + '<div class="ht-stat-label">' + esc(c[0]) + "</div>"
                + '<div class="ht-stat-value">' + num(c[1]) + "<small>" + esc(c[2]) + "</small></div>"
                + '<div class="ht-stat-sub">' + esc(c[3]) + "</div>"
                + "</div>";
        }).join("");
    }

    /* ---------------------------------------------------------------- 底部两栏 */
    function renderLatest(data) {
        var box = $("htLatest");
        var tip = $("htLatestTip");
        if (tip) {
            tip.textContent = (data.latest_date ? "数据截至 " + data.latest_date : "—")
                + " · 主线方向口径";
        }
        var rows = data.latest || [];
        if (!rows.length) {
            box.innerHTML = '<div class="ht-empty" style="min-height:120px;">'
                + (data.latest_date
                    ? esc(data.latest_date) + " 没有形成主线：没有任何方向凑齐 3 家以上涨停。"
                    : "还没有热点日报，收盘轮（21:34）跑完会自动生成。")
                + "</div>";
            return;
        }
        box.innerHTML = rows.map(function (r) {
            var stage = r.stage ? '<span class="ht-stage">' + esc(r.stage) + "</span>" : "";
            var meta = "涨停 <b>" + num(r.limit_up) + "</b> 家 · 最高 " + num(r.max_board)
                + " 板 · 新闻 " + num(r.news_count) + " 篇";
            var sub = r.sector_text
                ? '<div class="ht-latest-sub">细分：' + esc(r.sector_text) + "</div>"
                : "";
            return '<div class="ht-latest-item">'
                + '<span class="ht-rank-no">' + num(r.rank) + "</span>"
                + '<span class="ht-latest-name">' + esc(r.name) + "</span>" + stage
                + '<span class="ht-latest-meta">' + meta + "</span>"
                + "</div>" + sub;
        }).join("");
    }

    function renderRanking(data) {
        var box = $("htRanking");
        var rows = (data.ranking || []).slice(0, 8);
        if (!rows.length) {
            box.innerHTML = '<div class="ht-empty" style="min-height:120px;">还没有行业上过榜。</div>';
            return;
        }
        var max = Math.max.apply(null, rows.map(function (r) { return Number(r.count) || 0; })) || 1;
        box.innerHTML = rows.map(function (r, i) {
            var width = Math.round((Number(r.count) || 0) / max * 100);
            return '<div class="ht-rank-item">'
                + '<span class="ht-rank-no">' + (i + 1) + "</span>"
                + '<span class="ht-rank-name">' + esc(r.name) + "</span>"
                + '<span class="ht-rank-meta">累计 ' + num(r.count) + " 天 · 最长连续 "
                + num(r.streak) + " 天 · 峰值 " + num(r.best) + " 家</span>"
                + "</div>"
                + '<div class="ht-rank-bar"><i style="width:' + width + '%"></i></div>';
        }).join("");
    }

    /* ---------------------------------------------------------------- 点阵图 */
    function buildOption(view, data) {
        var dates = data.dates || [];
        var industries = view.industries || [];
        var points = view.points || [];
        var maxCount = view.max_count || 1;
        var latest = data.latest_date;
        var focus = view.focus || {};

        var dateIdx = {}, industryIdx = {};
        dates.forEach(function (d, i) { dateIdx[d] = i; });
        industries.forEach(function (n, i) { industryIdx[n] = i; });

        // 颜色只跟「这一行在纵轴的位置」有关，所以行业顺序改了，整张图的配色
        // 会自动跟着重排；关注方向被置顶后自然落在红端。
        function colorAt(i) {
            return gradientColor(industries.length <= 1 ? 0 : i / (industries.length - 1));
        }
        var labelRich = {};
        industries.forEach(function (n, i) {
            labelRich["c" + i] = {
                color: colorAt(i),
                fontSize: 11,
                fontWeight: focus[n] ? 700 : 400
            };
        });

        var scatterData = [];
        points.forEach(function (p) {
            if (dateIdx[p.date] === undefined || industryIdx[p.name] === undefined) return;
            var isLatest = p.date === latest;
            var color = colorAt(industryIdx[p.name]);
            scatterData.push({
                value: [dateIdx[p.date], industryIdx[p.name], p.count],
                meta: p,
                itemStyle: {
                    color: color,
                    borderColor: "#ffffff",
                    // 最新交易日：细白边 + 同行颜色的外发光。边框太粗会把小圆点啃成「空心圈」
                    borderWidth: isLatest ? 1.6 : 1,
                    shadowBlur: isLatest ? 8 : 0,
                    shadowColor: isLatest ? withAlpha(color, 0.55) : "transparent",
                    opacity: 0.96
                }
            });
        });

        var manyDates = dates.length > 12;
        var bottom = manyDates ? 66 : 46;

        var option = {
            animationDuration: 240,
            grid: { left: 112, right: 26, top: 16, bottom: bottom },
            tooltip: {
                trigger: "item", confine: true,
                backgroundColor: "rgba(255,255,255,0.98)",
                borderColor: "#e2e8f0", borderWidth: 1, padding: [9, 12],
                textStyle: { color: HT.ink, fontSize: 12, lineHeight: 19 },
                formatter: function (params) {
                    var m = params && params.data && params.data.meta;
                    if (!m) return "";
                    var html = "<b>" + esc(m.date) + "</b> · <b>" + esc(m.name) + "</b>"
                        + "<br/>涨停 <b>" + num(m.count) + "</b> 家 · 当日第 " + num(m.rank) + " 主线";
                    if (m.stage) html += " · " + esc(m.stage);
                    if (m.max_board) html += "<br/>最高 " + num(m.max_board) + " 板";
                    if (m.news_count) html += " · 相关新闻 " + num(m.news_count) + " 篇";
                    if (m.group && m.group !== m.name) {
                        html += "<br/>所属主线：" + esc(m.group) + "（" + num(m.parent_count) + " 家涨停）";
                    }
                    if (m.sector_text) {
                        html += '<br/><span style="color:#8a8f99">细分：' + esc(m.sector_text) + "</span>";
                    }
                    html += '<br/><span style="color:#9aa0ab">口径：' + MODE_LABEL + "</span>";
                    return html;
                }
            },
            xAxis: {
                type: "category", data: dates, boundaryGap: true,
                axisLine: { lineStyle: { color: HT.axis } },
                axisTick: { show: false },
                axisLabel: {
                    color: HT.sub, fontSize: 10, hideOverlap: true, showMaxLabel: true,
                    // 最新交易日的日期标红加粗，和该列圆点的白边一起指向「当日」
                    formatter: function (v) {
                        var text = String(v).slice(5);
                        return v === latest ? "{latest|" + text + "}" : text;
                    },
                    rich: { latest: { color: "#c0392b", fontWeight: 600, fontSize: 11 } }
                },
                splitLine: { show: true, lineStyle: { color: HT.grid, width: 1 } }
            },
            yAxis: {
                type: "category", data: industries, inverse: true,
                axisLine: { show: false }, axisTick: { show: false },
                // 标签逐行着色，与圆点同源，从上到下形成红→蓝渐变；关注方向加粗
                axisLabel: {
                    margin: 12,
                    formatter: function (v, i) { return "{c" + i + "|" + v + "}"; },
                    rich: labelRich
                },
                splitLine: { show: true, lineStyle: { color: HT.grid, width: 1 } }
            },
            series: [{
                type: "scatter",
                data: scatterData,
                symbolSize: function (value) { return dotSize(value[2], maxCount); },
                emphasis: { scale: 1.3, itemStyle: { opacity: 1 } }
            }]
        };

        // 日期少时滑块纯属占地方，只留滚轮缩放；日期多了才给底部拖动条
        option.dataZoom = manyDates
            ? [
                { type: "inside", xAxisIndex: 0, zoomOnMouseWheel: true, moveOnMouseWheel: false },
                {
                    type: "slider", xAxisIndex: 0,
                    bottom: 6, height: 20, left: 112, right: 26,
                    borderColor: "#e6e8ee", backgroundColor: "#fbfcfe",
                    fillerColor: "rgba(120,90,170,0.10)",
                    handleStyle: { color: "#fff", borderColor: "#aeb4bf", borderWidth: 1 },
                    textStyle: { color: HT.sub, fontSize: 10 }
                }
            ]
            : [{ type: "inside", xAxisIndex: 0, zoomOnMouseWheel: true, moveOnMouseWheel: false }];

        return option;
    }

    function disposeChart() {
        if (state.chart) {
            state.chart.dispose();
            state.chart = null;
        }
    }

    function mountChart(view, data) {
        var dom = $("htChart");
        if (!dom) return;

        if (!view || !view.industries || !view.industries.length) {
            disposeChart();
            dom.style.height = "";
            dom.innerHTML = '<div class="ht-empty">'
                + (data.summary && data.summary.report_days
                    ? "这段区间里还没有任何方向形成过主线。收盘轮每天 21:34 生成日报，有主线后这里会自动出现圆点。"
                    : "还没有热点日报。收盘轮（21:34）跑完会自动生成，之后每个交易日出现一列新的圆点。")
                + "</div>";
            return;
        }

        if (typeof echarts === "undefined" || typeof echarts.init !== "function") {
            disposeChart();
            dom.innerHTML = '<div class="chart-load-failed">图表库加载失败，请刷新重试</div>';
            return;
        }

        // 每行 34px：展示全部历史后行业数会随交易日累积，固定高度会把点压成
        // 一条缝；宁可让页面长一点，也不要挤到看不清（上限放到 2400 ≈ 70 行）。
        var height = Math.max(320, Math.min(2400, 100 + view.industries.length * 34));
        dom.style.height = height + "px";
        disposeChart();
        dom.innerHTML = "";
        state.chart = echarts.init(dom);
        state.chart.setOption(buildOption(view, data), true);

        var tip = $("htChartTip");
        if (tip) {
            var focusCount = view.focus ? Object.keys(view.focus).length : 0;
            tip.textContent = MODE_LABEL + " · "
                + view.industries.length + " 个行业 · " + view.points.length + " 个圆点"
                + (focusCount ? " · 已置顶 " + focusCount + " 个关注方向" : "")
                + (view.industries.length > 24 ? " · 行业较多，向下滚动可看全" : "");
        }
    }

    /* ---------------------------------------------------------------- 组装 */
    function render() {
        var data = state.data;
        if (!data) return;
        var view = (data.views || {})[FIXED_MODE] || data.views.group;
        renderStats(data, view);
        renderLatest(data);
        renderRanking(data);

        // 图例渐变条与圆点共用同一个色函数，避免 CSS 里另写一套色标后跑偏
        var bar = document.querySelector(".ht-gradient");
        if (bar) {
            bar.style.background = "linear-gradient(90deg,"
                + [0, 0.2, 0.4, 0.5, 0.6, 0.8, 1].map(gradientColor).join(",") + ")";
        }
        var tip = $("htChartTip");
        if (tip && (!view || !view.industries || !view.industries.length)) {
            tip.textContent = "暂无可绘制的行业";
        }
        mountChart(view, data);

        $("htUpdated").textContent = data.latest_date
            ? "数据截至 " + data.latest_date + " · 共 " + num((data.summary || {}).report_days) + " 个交易日"
            : "尚无数据";

        if ((data.summary || {}).report_days > 0 && (data.summary || {}).report_days < 4) {
            notice("warn", "历史正在积累：热点日报从该表启用之日开始保存，不会回填更早的交易日。"
                + "目前只有 " + num(data.summary.report_days) + " 天数据，趋势需要多几个交易日才看得出来。");
        } else {
            notice("", "");
        }
    }

    /* ---------------------------------------------------------------- 取数 */
    async function load() {
        if (state.busy) return;
        state.busy = true;
        try {
            var resp = await fetchWithAuth("/api/hotspot/trend?days=" + FIXED_DAYS);
            var data = await resp.json();
            if (data.status !== "ok") {
                throw new Error(data.message || "加载失败");
            }
            state.data = data;
            render();
        } catch (err) {
            notice("error", err && err.message ? err.message : "加载失败");
            $("htStats").innerHTML = "";
            $("htChart").innerHTML = '<div class="ht-empty">加载失败，请稍后刷新页面重试。</div>';
        } finally {
            state.busy = false;
        }
    }

    /* ---------------------------------------------------------------- 启动 */
    document.addEventListener("DOMContentLoaded", function () {
        window.addEventListener("resize", debounce(function () {
            if (state.chart) state.chart.resize();
        }, 180));
        load();
    });
})();
