/* ==========================================================================
   今日热点（/today）
   --------------------------------------------------------------------------
   数据只有一次请求：GET /api/hotspot/today（整份日报在 payload 里）。
   区块顺序与后端 payload 字段一一对应：
     tone / verdict  →  A 今日定调
     themes          →  B 今日主线
     stocks/boards   →  C 热股三榜、D 资金地图
     timeline        →  E 今日大事
   ========================================================================== */
(function () {
    "use strict";

    var state = {
        date: "",
        dates: [],
        payload: null,
        busy: false
    };

    function $(id) { return document.getElementById(id); }

    function esc(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function num(value, digits) {
        if (value === null || value === undefined || value === "" || isNaN(Number(value))) return "—";
        return Number(value).toFixed(digits === undefined ? 1 : digits);
    }

    // 涨红跌绿（A 股惯例）
    function pctClass(value) {
        var v = Number(value);
        if (isNaN(v) || value === null) return "th-flat";
        if (v > 0) return "th-up";
        if (v < 0) return "th-down";
        return "th-flat";
    }

    function pctText(value) {
        if (value === null || value === undefined || isNaN(Number(value))) return "—";
        var v = Number(value);
        return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
    }

    function yiText(value) {
        if (value === null || value === undefined || isNaN(Number(value))) return "—";
        var v = Number(value);
        return (v > 0 ? "+" : "") + v.toFixed(1) + "亿";
    }

    function notice(kind, text) {
        var box = $("thNotice");
        if (!box) return;
        if (!text) { box.innerHTML = ""; return; }
        box.innerHTML = '<div class="th-notice ' + kind + '">' + esc(text) + "</div>";
    }

    // ---------------------------------------------------------------- 缺省值
    function metric(label, value, sub, cls) {
        return '<div class="th-metric">'
            + '<div class="th-metric-label">' + esc(label) + "</div>"
            + '<div class="th-metric-value ' + (cls || "") + '">' + value + "</div>"
            + '<div class="th-metric-sub">' + (sub || "&nbsp;") + "</div>"
            + "</div>";
    }

    function renderTone(payload) {
        var tone = payload.tone || {};
        var limit = tone.limit || {};
        var breadth = tone.breadth;
        var temp = tone.temperature;
        var cards = [];

        (tone.index || []).forEach(function (row) {
            cards.push(metric(
                row.name,
                num(row.value, 2),
                '<span class="' + pctClass(row.change_pct) + '">' + pctText(row.change_pct) + "</span>",
                pctClass(row.change_pct)
            ));
        });

        var turnover = tone.turnover || {};
        var deltaSub = "—";
        if (turnover.delta !== null && turnover.delta !== undefined) {
            var up = Number(turnover.delta) > 0;
            deltaSub = '<span class="' + (up ? "th-up" : "th-down") + '">'
                + (up ? "放量 " : "缩量 ") + Math.abs(Number(turnover.delta)).toFixed(0) + "亿</span>";
        }
        cards.push(metric("两市成交", turnover.value === null || turnover.value === undefined
            ? "—" : num(turnover.value, 0) + "亿", deltaSub));

        var breadthText = breadth === null || breadth === undefined ? "—" : num(breadth, 0) + "%";
        var breadthTag = "—";
        if (typeof breadth === "number") {
            breadthTag = breadth >= 80 ? "普涨" : breadth >= 60 ? "偏强"
                : breadth >= 40 ? "结构分化" : "普跌";
        }
        cards.push(metric("赚钱效应", breadthText, breadthTag));

        cards.push(metric(
            "涨停 / 跌停",
            '<span class="th-up">' + (limit.up || 0) + "</span> / "
            + '<span class="th-down">' + (limit.down || 0) + "</span>",
            limit.seal_rate === null || limit.seal_rate === undefined
                ? "炸板 " + (limit.broken || 0) + " 家"
                : "封板率 " + num(limit.seal_rate, 0) + "% · 炸板 " + (limit.broken || 0)
        ));

        cards.push(metric(
            "最高连板",
            (limit.max_board || 0) + " 板",
            limit.promotion === null || limit.promotion === undefined
                ? "晋级率 —" : "晋级率 " + num(limit.promotion, 0) + "%"
        ));

        var tempTag = "—";
        if (typeof temp === "number") {
            tempTag = temp >= 65 ? "偏热" : temp >= 40 ? "中性" : "偏冷";
        }
        cards.push(metric("情绪温度", temp === null || temp === undefined ? "—" : num(temp, 0), tempTag));

        return '<div class="th-card th-tone">'
            + '<div class="th-metrics">' + cards.join("") + "</div>"
            + '<div class="th-verdict">' + esc(payload.verdict || "—") + "</div>"
            + "</div>";
    }

    // ---------------------------------------------------------------- B 主线
    var STAGE_CLASS = { "启动": "s-start", "加速": "s-accel", "延续": "s-cont", "分歧": "s-diverge" };

    function renderThemes(themes) {
        if (!themes || !themes.length) {
            return '<div class="th-card"><div class="th-empty" style="border:none;padding:18px;">'
                + "<p>今天没有形成主线：没有任何方向凑齐 3 家以上涨停。"
                + "这种日子通常是无主线混沌轮动，别硬找。</p></div></div>";
        }

        var html = themes.map(function (theme) {
            var stage = theme.stage
                ? '<span class="th-stage ' + (STAGE_CLASS[theme.stage] || "s-cont") + '">' + esc(theme.stage) + "</span>"
                : "";
            var sectors = (theme.sectors || []).map(function (s) {
                return '<span class="th-chip">' + esc(s.name) + " " + s.count + "</span>";
            }).join("");
            var leads = (theme.lead_stocks || []).slice(0, 6).map(function (s) {
                return '<span class="th-lead">' + esc(s.name)
                    + (s.board > 1 ? "<small>" + s.board + "板</small>" : "")
                    + "</span>";
            }).join("");

            var daysNote = theme.days_on_list > 0
                ? "连续 " + (theme.days_on_list + 1) + " 日上榜"
                : "今日首次上榜";

            return '<div class="th-theme">'
                + '<div class="th-theme-head"><span class="th-theme-name">' + esc(theme.name) + "</span>" + stage + "</div>"
                + '<div class="th-theme-stats">'
                + "<span>涨停 <b>" + theme.limit_up + "</b> 家</span>"
                + "<span>最高 <b>" + theme.max_board + "</b> 板</span>"
                + "<span>封单 <b>" + num(theme.seal_fund_yi, 1) + "</b> 亿</span>"
                + "<span>相关新闻 <b>" + (theme.news_count || 0) + "</b> 篇</span>"
                + "</div>"
                + '<div class="th-chips">' + sectors + "</div>"
                + (leads ? '<div class="th-leads">' + leads + "</div>" : "")
                + '<div class="th-metric-sub">' + esc(daysNote) + "</div>"
                + "</div>";
        }).join("");

        return '<div class="th-themes">' + html + "</div>";
    }

    // ---------------------------------------------------------------- C 热股
    function stockRows(rows, sign) {
        if (!rows || !rows.length) return '<div class="th-metric-sub">暂无数据</div>';
        return rows.map(function (r) {
            return '<div class="th-row">'
                + '<span class="th-row-name">' + esc(r.name)
                + (r.change_pct !== null && r.change_pct !== undefined
                    ? '<small class="' + pctClass(r.change_pct) + '">' + pctText(r.change_pct) + "</small>" : "")
                + "</span>"
                + '<span class="th-row-val ' + (sign > 0 ? "th-up" : "th-down") + '">'
                + esc(yiText(r.net_inflow_yi)) + "</span>"
                + "</div>";
        }).join("");
    }

    function renderLadder(ladder, firstBoard) {
        var parts = [];
        (ladder || []).forEach(function (group) {
            var items = (group.items || []).map(function (s) {
                return '<span class="th-lead">' + esc(s.name)
                    + '<small>' + esc(s.sector || "") + "</small></span>";
            }).join("");
            parts.push('<div class="th-ladder-group">'
                + '<div class="th-ladder-title"><b>' + group.board + " 板</b> · " + group.count + " 家</div>"
                + '<div class="th-ladder-items">' + items + "</div></div>");
        });
        if (firstBoard) {
            parts.push('<div class="th-ladder-group"><div class="th-ladder-title"><b>首板</b> · '
                + firstBoard + " 家（不逐只列出）</div></div>");
        }
        if (!parts.length) return '<div class="th-metric-sub">今天没有连板股。</div>';
        return parts.join("");
    }

    function renderThemeRank(themes) {
        var sorted = (themes || []).slice().sort(function (a, b) {
            return (b.news_count || 0) - (a.news_count || 0) || b.limit_up - a.limit_up;
        });
        if (!sorted.length) return '<div class="th-metric-sub">暂无数据</div>';
        return sorted.map(function (t) {
            return '<div class="th-row">'
                + '<span class="th-row-name">' + esc(t.name) + "</span>"
                + '<span class="th-row-val">' + (t.news_count || 0) + " 篇 · "
                + t.limit_up + " 家涨停</span></div>";
        }).join("");
    }

    function renderStocks(stocks, themes) {
        stocks = stocks || {};
        return '<div class="th-card">'
            + '<div class="th-tabs">'
            + '<button class="th-tab active" data-pane="paneFund">资金榜</button>'
            + '<button class="th-tab" data-pane="paneLadder">涨停梯队</button>'
            + '<button class="th-tab" data-pane="paneTheme">题材热度</button>'
            + "</div>"
            + '<div class="th-pane active" id="paneFund"><div class="th-rows">'
            + '<div><div class="th-list-head">主力净流入</div>' + stockRows(stocks.inflow, 1) + "</div>"
            + '<div><div class="th-list-head">主力净流出</div>' + stockRows(stocks.outflow, -1) + "</div>"
            + "</div></div>"
            + '<div class="th-pane" id="paneLadder">' + renderLadder(stocks.ladder, stocks.first_board_count) + "</div>"
            + '<div class="th-pane" id="paneTheme">' + renderThemeRank(themes) + "</div>"
            + "</div>";
    }

    // ---------------------------------------------------------------- D 资金
    var TAG_CLASS = {
        "明牌启动": "tag-start", "暗中建仓": "tag-build", "撤退": "tag-exit",
        "资金流入": "tag-in", "反抽": "tag-reb"
    };

    function boardRows(rows, sign, emptyText) {
        if (!rows || !rows.length) {
            return '<div class="th-metric-sub">' + esc(emptyText || "暂无数据") + "</div>";
        }
        return rows.map(function (r) {
            var tag = r.tag
                ? '<span class="th-board-tag ' + (TAG_CLASS[r.tag] || "tag-in") + '">' + esc(r.tag) + "</span>"
                : "";
            // 粗行业名旁边挂大方向（电子信息 → 电子），方便与「今日主线」对上
            var group = (r.group && r.group !== r.name)
                ? '<span class="th-board-group">' + esc(r.group) + "</span>"
                : "";
            return '<div class="th-row">'
                + '<span class="th-row-name">' + esc(r.name) + " " + group + tag + "</span>"
                + '<span class="th-row-val ' + pctClass(sign > 0 ? 1 : -1) + '">'
                + esc(yiText(r.net_inflow_yi))
                + '<small style="color:#a4a9b3;margin-left:6px;">' + pctText(r.change_pct) + "</small>"
                + "</span></div>";
        }).join("");
    }

    function renderBoards(boards, fundSource) {
        boards = boards || {};
        var note = fundSource
            ? '<span class="th-fund-src">资金数据来源：' + esc(fundSource)
                + (fundSource === "新浪财经" ? "（板块为全部成交净额口径，与东财主力净额不可比）" : "（主力净额口径）")
                + "</span>"
            : "";
        return '<div class="th-card">'
            + '<div class="th-section-title" style="margin-bottom:12px;">资金去向 <em>涨跌 × 主力净额</em></div>'
            + '<div class="th-board-list"><div class="th-list-head">行业 · 净流入前列</div>'
            + boardRows(boards.inflow, 1) + "</div>"
            + '<div class="th-board-list"><div class="th-list-head">行业 · 净流出前列</div>'
            + boardRows(boards.outflow, -1, "今天各行业均为净流入") + "</div>"
            + '<div class="th-board-list"><div class="th-list-head">概念 · 净流入前列</div>'
            + boardRows(boards.concepts, 1) + "</div>"
            + note
            + '<div class="th-legend">'
            + "<b>明牌启动</b> 价涨钱进 · <b>暗中建仓</b> 钱进价跌 · <b>撤退</b> 价钱齐跌 · "
            + "<b>资金流入</b> 钱进但涨幅不足 1% · <b>反抽</b> 跌势中资金回流"
            + "</div></div>";
    }

    // ---------------------------------------------------------------- E 时间轴
    var NEWS_TAG_CLASS = { "政策": "tag-policy", "资金": "tag-fund", "数据": "tag-data", "产业": "tag-industry" };

    function renderTimeline(items) {
        if (!items || !items.length) {
            return '<div class="th-card"><div class="th-metric-sub">'
                + "今天还没抓到符合「影响面大」的新闻（或新闻域今天未运行）。</div></div>";
        }
        return '<div class="th-card"><div class="th-timeline">'
            + items.map(function (n) {
                var tag = n.tag
                    ? '<span class="th-news-tag ' + (NEWS_TAG_CLASS[n.tag] || "tag-industry") + '">' + esc(n.tag) + "</span>"
                    : "";
                var title = n.url
                    ? '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.title) + "</a>"
                    : esc(n.title);
                return '<div class="th-news-item"><span class="th-news-time">' + esc(n.time || "")
                    + "</span>" + tag + "<span>" + title + "</span></div>";
            }).join("")
            + "</div></div>";
    }

    // ---------------------------------------------------------------- 组装
    function render(payload) {
        var html = "";

        if (payload.data_gaps && payload.data_gaps.length) {
            html += '<div class="th-gaps"><b>部分数据源本次没抓到</b>：'
                + payload.data_gaps.map(esc).join("；")
                + "。其余区块照常展示，缺的部分显示为「—」。</div>";
        }

        html += '<div class="th-section">' + renderTone(payload) + "</div>";
        html += '<div class="th-section"><div class="th-section-title">今日主线'
            + "<em>涨停股所属行业聚簇 × 当日新闻命中，家数 ≥3 才上榜</em></div>"
            + renderThemes(payload.themes) + "</div>";
        html += '<div class="th-section th-split">'
            + '<div><div class="th-section-title">热股三榜<em>资金 / 连板 / 题材</em></div>'
            + renderStocks(payload.stocks, payload.themes) + "</div>"
            + "<div>" + renderBoards(payload.boards, payload.fund_source) + "</div>"
            + "</div>";
        html += '<div class="th-section"><div class="th-section-title">今日大事'
            + "<em>只留影响面大的，最多 8 条</em></div>"
            + renderTimeline(payload.timeline) + "</div>";

        $("thRoot").innerHTML = html;
        bindTabs();
    }

    function bindTabs() {
        var tabs = document.querySelectorAll(".th-tab");
        Array.prototype.forEach.call(tabs, function (tab) {
            tab.addEventListener("click", function () {
                Array.prototype.forEach.call(tabs, function (t) { t.classList.remove("active"); });
                tab.classList.add("active");
                var panes = document.querySelectorAll(".th-pane");
                Array.prototype.forEach.call(panes, function (p) { p.classList.remove("active"); });
                var target = $(tab.getAttribute("data-pane"));
                if (target) target.classList.add("active");
            });
        });
    }

    function renderEmpty(message) {
        $("thRoot").innerHTML = '<div class="th-empty">'
            + "<h3>这一天还没有热点日报</h3>"
            + "<p>" + esc(message || "收盘轮（21:34）跑完会自动生成。") + "</p>"
            + '<button class="btn btn-primary" id="thEmptyGen">立即生成</button>'
            + "</div>";
        var btn = $("thEmptyGen");
        if (btn) btn.addEventListener("click", doRefresh);
    }

    function fillDates(dates, current) {
        var select = $("thDate");
        if (!select) return;
        if (!dates || !dates.length) {
            select.innerHTML = "<option>暂无日报</option>";
            select.disabled = true;
            return;
        }
        select.disabled = false;
        select.innerHTML = dates.map(function (d) {
            return '<option value="' + esc(d) + '"' + (d === current ? " selected" : "") + ">"
                + esc(d) + "</option>";
        }).join("");
    }

    // ---------------------------------------------------------------- 取数
    async function load(date) {
        var url = "/api/hotspot/today" + (date ? "?date=" + encodeURIComponent(date) : "");
        try {
            var resp = await fetchWithAuth(url);
            var data = await resp.json();
            notice("", "");

            if (data.status !== "ok") {
                state.dates = data.dates || [];
                fillDates(state.dates, "");
                $("thUpdated").textContent = "尚无数据";
                renderEmpty(data.message);
                return;
            }

            state.dates = data.dates || [];
            state.payload = data.payload;
            state.date = data.date;
            fillDates(state.dates, data.date);
            $("thUpdated").textContent = "数据截至 " + data.date
                + (data.generated_at ? " · 生成于 " + data.generated_at : "");
            render(data.payload);
        } catch (err) {
            notice("error", err && err.message ? err.message : "加载失败");
            $("thRoot").innerHTML = '<div class="th-empty"><h3>加载失败</h3>'
                + "<p>请稍后刷新页面重试。</p></div>";
        }
    }

    async function doRefresh() {
        if (state.busy) return;
        state.busy = true;
        var btns = [$("thRefresh"), $("thEmptyGen")].filter(Boolean);
        btns.forEach(function (b) { b.disabled = true; b.textContent = "生成中…"; });
        notice("ok", "正在抓取涨停池与资金流并生成日报，约需 10~25 秒，请不要关闭页面…");

        try {
            var resp = await fetchWithAuth("/api/hotspot/refresh", { method: "POST" });
            var data = await resp.json();
            if (data.status === "ok") {
                var s = data.summary || {};
                notice("ok", "已生成 " + (s.market_date || "") + " 的日报：涨停 " + (s.limit_up || 0)
                    + " 家，主线 " + ((s.themes || []).join("、") || "无"));
                await load(s.market_date || state.date);
            } else if (data.status === "running") {
                notice("error", data.message || "正在生成中，请稍候");
            } else {
                notice("error", data.message || "生成失败");
            }
        } catch (err) {
            notice("error", err && err.message ? err.message : "生成失败");
        } finally {
            state.busy = false;
            btns.forEach(function (b) {
                b.disabled = false;
                b.textContent = b.id === "thEmptyGen" ? "立即生成" : "重新生成";
            });
        }
    }

    // ---------------------------------------------------------------- 启动
    document.addEventListener("DOMContentLoaded", function () {
        var select = $("thDate");
        if (select) {
            select.addEventListener("change", function () {
                load(select.value);
            });
        }
        var btn = $("thRefresh");
        if (btn) btn.addEventListener("click", doRefresh);
        load("");
    });
})();
