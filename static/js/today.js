/* ==========================================================================
   今日热点（/today）
   --------------------------------------------------------------------------
   数据只有一次请求：GET /api/hotspot/today（整份日报在 payload 里）。
   区块顺序与后端 payload 字段一一对应：
     tone / verdict  →  A 今日定调
     llm_summary     →  A2 AI 盘面归因（另有一个 llm-config 请求）
     themes          →  B 今日主线
     stocks/boards   →  C 热股三榜、D 资金地图
     timeline        →  E 今日大事

   AI 归因按交易日缓存在 payload.llm_summary 里（version>=2 才是新版），
   页面只读快照；只有「生成 / 重新生成」才调模型，配置状态单独走 llm-config。
   新版读全量原始证据 + 近 5 日档案、两轮自检，输出反常信号/归因假设/验证清单。
   ========================================================================== */
(function () {
    "use strict";

    var state = {
        payload: null,
        busy: false,
        llm: {
            config: null,      // GET /api/hotspot/llm-config 的结果（不含完整 Key）
            ready: false,
            busy: false,
            error: "",
            note: "",
            showCfg: false,
            keyDraft: ""
        }
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
            // 温度打卡与日报不同日（比如跨周末手动刷新）就标出实际日期，别当今日读
            if (tone.temperature_date && payload.market_date
                && tone.temperature_date !== payload.market_date) {
                tempTag += " · " + tone.temperature_date.slice(5);
            }
        }
        cards.push(metric("情绪温度", temp === null || temp === undefined ? "—" : num(temp, 0), tempTag));

        return '<div class="th-card th-tone">'
            + '<div class="th-metrics">' + cards.join("") + "</div>"
            + '<div class="th-verdict">' + esc(payload.verdict || "—") + "</div>"
            + "</div>";
    }

    // ---------------------------------------------------------------- B 主线
    var STAGE_CLASS = { "启动": "s-start", "加速": "s-accel", "延续": "s-cont", "分歧": "s-diverge" };

    // 今日主线只展示排名前 3 的方向（后端已按涨停家数排序 + 相对阈值过滤）。
    // 截断只发生在这一块卡片的渲染层：payload 里仍保留全部达标方向，
    // 热点趋势点阵和「题材热度」榜要读全量，不能在生成日报时砍数据。
    var THEME_LIMIT = 3;

    function renderThemes(themes) {
        var all = themes || [];
        if (!all.length) {
            return '<div class="th-card"><div class="th-empty" style="border:none;padding:18px;">'
                + "<p>今天没有形成主线：没有任何方向凑齐 3 家以上涨停。"
                + "这种日子通常是无主线混沌轮动，别硬找。</p></div></div>";
        }
        var shown = all.slice(0, THEME_LIMIT);

        var html = shown.map(function (theme) {
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

    // ------------------------------------------------------------ A2 AI 解读
    function llmConfig() { return state.llm.config || {}; }

    function confText(v) {
        var map = { high: "较高", medium: "中", low: "偏低" };
        return map[String(v || "").toLowerCase()] || "中";
    }

    function isV2(s) { return !!s && Number(s.version || 1) >= 2; }

    function renderLlmBody() {
        var llm = state.llm;
        var cfg = llmConfig();
        if (llm.busy) {
            return '<div class="th-llm-loading"><span class="th-llm-dot"></span>'
                + "正在读全量盘面证据与近 5 日档案，两轮归因自检约需 15~30 秒…</div>";
        }
        if (llm.error) {
            return '<div class="th-llm-error">' + esc(llm.error) + "</div>";
        }
        var s = (state.payload || {}).llm_summary;
        if (s && !isV2(s)) {
            return '<div class="th-llm-empty"><b>解读已升级为「归因分析」。</b>'
                + "旧版只是把定调数字换成句子，与页面上的规则总结重复。"
                + "点右上角「升级解读」重新生成：读新闻标题、连板梯队、首次封板时间、炸板次数、资金地图与近 5 日档案，"
                + "经两轮自检输出反常信号、带证据链与反证的归因假设、可证伪的验证清单。</div>";
        }
        if (isV2(s)) {
            var html = "";
            var a = s.anomaly || {};
            if (a.signal) {
                html += '<div class="th-llm-anomaly"><span class="th-llm-label">最反常的信号</span>'
                    + '<div class="th-llm-anomaly-signal">' + esc(a.signal) + "</div>"
                    + (a.why ? '<div class="th-llm-anomaly-why">' + esc(a.why) + "</div>" : "")
                    + "</div>";
            }
            var hyps = s.hypotheses || [];
            if (hyps.length) {
                html += '<div class="th-llm-block"><span class="th-llm-label">归因假设 · 证据链与反证</span>'
                    + '<div class="th-llm-hyps">';
                hyps.forEach(function (h, i) {
                    html += '<div class="th-llm-hyp"><div class="th-llm-hyp-claim">'
                        + '<span class="th-llm-hyp-no">' + ["①", "②", "③"][i] + "</span>"
                        + esc(h.claim)
                        + '<span class="th-llm-conf">置信 ' + esc(confText(h.confidence)) + "</span>"
                        + "</div>";
                    var ev = (h.evidence || []).map(function (e) {
                        return "<li>" + esc(e) + "</li>";
                    }).join("");
                    if (ev) html += '<ul class="th-llm-evidence">' + ev + "</ul>";
                    if (h.counter) {
                        html += '<div class="th-llm-counter"><b>反证</b> ' + esc(h.counter) + "</div>";
                    }
                    html += "</div>";
                });
                html += "</div></div>";
            }
            if (s.overlooked || s.cycle) {
                html += '<div class="th-llm-notes">';
                if (s.overlooked) {
                    html += '<div class="th-llm-note"><span>被忽略的信号</span>'
                        + esc(s.overlooked) + "</div>";
                }
                if (s.cycle) {
                    html += '<div class="th-llm-note"><span>周期定位</span>'
                        + esc(s.cycle) + "</div>";
                }
                html += "</div>";
            }
            var checks = s.checks || [];
            if (checks.length) {
                html += '<div class="th-llm-block"><span class="th-llm-label">明日验证清单</span>'
                    + '<div class="th-llm-checks">';
                checks.forEach(function (c) {
                    html += '<div class="th-llm-check"><b>若</b> ' + esc(c["if"])
                        + ' <b>则</b> ' + esc(c.then) + "</div>";
                });
                html += "</div></div>";
            }
            html += '<div class="th-llm-foot">由 '
                + esc(s.model || cfg.model || "DeepSeek Flash")
                + " 基于全量原始证据 + 近 " + esc(s.history_days || 0) + " 日档案"
                + (s.reviewed === false ? "（审稿轮降级，仅首轮）" : "，经两轮自检")
                + "生成"
                + (s.generated_at ? " · " + esc(s.generated_at) : "")
                + (s.total_tokens ? " · " + esc(String(s.total_tokens)) + " tokens" : "")
                + " · 基于当日数据的推断，非事实陈述，不构成投资建议</div>";
            return html;
        }
        if (!llm.ready) return '<div class="th-llm-empty">正在检查模型配置…</div>';
        if (!cfg.configured) {
            return '<div class="th-llm-empty"><b>还没有配置 DeepSeek API Key。</b>'
                + "点右上角「设置 Key」填入 sk-… 后立即生效；"
                + "也可以写进项目根目录 .env 的 DEEPSEEK_API_KEY 再重新发布。"
                + "<br>配置好之后点「生成解读」，模型会读全量盘面证据做归因分析，而不是复述数字。</div>";
        }
        return '<div class="th-llm-empty">这一天的日报还没有 AI 归因分析。'
            + "点「生成解读」：喂入新闻标题、连板梯队、首次封板时间、炸板次数、资金地图与近 5 日档案，"
            + "经两轮自检输出反常信号、带证据链与反证的归因假设、可证伪的验证清单。"
            + "生成后按交易日缓存，收盘轮产出新日报时自动更新。</div>";
    }

    function renderLlmCard() {
        var box = $("thLlm");
        if (!box) return;
        var llm = state.llm;
        var cfg = llmConfig();
        var s = (state.payload || {}).llm_summary;
        var meta;
        if (s && s.generated_at) meta = (isV2(s) ? "归因于 " : "旧版解读 · ") + esc(s.generated_at);
        else if (llm.ready && !cfg.configured) meta = "Key 未配置";
        else meta = esc(llm.note || "尚无解读");

        var cfgPanel = '<div class="th-llm-cfg" id="thLlmCfgBox" style="display:'
            + (llm.showCfg ? "flex" : "none") + '">'
            + '<input id="thLlmKey" type="password" autocomplete="off" spellcheck="false"'
            + ' placeholder="粘贴 DeepSeek API Key（sk-…）" value="' + esc(llm.keyDraft) + '">'
            + '<button class="th-llm-btn" id="thLlmSave"' + (llm.busy ? " disabled" : "") + ">保存并生效</button>"
            + '<button class="th-llm-link" id="thLlmCancel">取消</button>'
            + '<p class="th-llm-cfg-hint">Key 只保存在服务端，页面不会回显完整值'
            + (cfg.key_hint ? "（当前 " + esc(cfg.key_hint) + "）" : "")
            + "。云沙箱是临时文件系统，重启后可能需重填；长期生效请写入 .env。</p>"
            + "</div>";

        box.innerHTML = '<div class="th-llm-head">'
            + '<div class="th-llm-title"><span class="th-llm-mark">✦</span> AI 盘面归因'
            + '<span class="th-llm-badge">' + esc(cfg.model || "DeepSeek Flash") + "</span></div>"
            + '<div class="th-llm-actions">'
            + '<span class="th-llm-meta">' + meta + "</span>"
            + '<button class="th-llm-btn" id="thLlmGen"'
            + (llm.busy || !llm.ready || !state.payload ? " disabled" : "") + ">"
            + (s && !isV2(s) ? "升级解读" : (s ? "重新生成" : "生成解读")) + "</button>"
            + '<button class="th-llm-link" id="thLlmCfg">设置 Key</button>'
            + "</div></div>"
            + cfgPanel
            + '<div class="th-llm-body">' + renderLlmBody() + "</div>";
        bindLlm();
    }

    function bindLlm() {
        var gen = $("thLlmGen");
        if (gen) gen.addEventListener("click", function () { doLlmGenerate(true); });
        var cfgBtn = $("thLlmCfg");
        if (cfgBtn) cfgBtn.addEventListener("click", function () {
            state.llm.showCfg = !state.llm.showCfg;
            state.llm.error = "";
            renderLlmCard();
            if (state.llm.showCfg) { var f = $("thLlmKey"); if (f) f.focus(); }
        });
        var input = $("thLlmKey");
        if (input) {
            input.addEventListener("input", function () { state.llm.keyDraft = input.value; });
            input.addEventListener("keydown", function (e) {
                if (e.key === "Enter") saveLlmKey();
            });
        }
        var save = $("thLlmSave");
        if (save) save.addEventListener("click", saveLlmKey);
        var cancel = $("thLlmCancel");
        if (cancel) cancel.addEventListener("click", function () {
            state.llm.showCfg = false;
            renderLlmCard();
        });
    }

    async function loadLlmConfig() {
        try {
            var resp = await fetchWithAuth("/api/hotspot/llm-config");
            state.llm.config = await resp.json();
        } catch (err) {
            state.llm.config = { configured: false, model: "deepseek-flash" };
            state.llm.error = "配置状态读取失败：" + (err && err.message ? err.message : "网络异常");
        }
        state.llm.ready = true;
        renderLlmCard();
    }

    async function doLlmGenerate(force) {
        if (state.llm.busy) return;
        state.llm.busy = true;
        state.llm.error = "";
        state.llm.note = "";
        renderLlmCard();
        try {
            var resp = await fetchWithAuth(
                "/api/hotspot/llm-summary?force=" + (force ? "true" : "false"),
                { method: "POST" });
            var data = await resp.json();
            if (data.status === "ok") {
                if (state.payload) state.payload.llm_summary = data.summary;
                state.llm.note = data.cached ? "已显示缓存解读" : "解读已生成并缓存";
            } else if (data.status === "unconfigured") {
                state.llm.config = Object.assign({}, llmConfig(), { configured: false });
                state.llm.showCfg = true;
                state.llm.error = data.message || "尚未配置 API Key";
            } else {
                state.llm.error = data.message || "生成失败";
            }
        } catch (err) {
            state.llm.error = err && err.message ? err.message : "生成失败";
        } finally {
            state.llm.busy = false;
            renderLlmCard();
        }
    }

    async function saveLlmKey() {
        if (state.llm.busy) return;
        var key = (state.llm.keyDraft || "").trim();
        if (!key) {
            state.llm.error = "请先粘贴 API Key（sk-…）";
            renderLlmCard();
            return;
        }
        state.llm.busy = true;
        state.llm.error = "";
        renderLlmCard();
        var saved = false;
        try {
            var resp = await fetchWithAuth("/api/hotspot/llm-config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ api_key: key })
            });
            var data = await resp.json();
            if (data.status === "ok") {
                saved = true;
                state.llm.config = data;
                state.llm.showCfg = false;
                state.llm.keyDraft = "";
                state.llm.note = data.persisted
                    ? "Key 已写入 .env，重启后仍有效"
                    : "Key 已在当前进程生效（沙箱临时存储，重启后需重填）";
            } else {
                state.llm.error = data.message || "保存失败";
            }
        } catch (err) {
            state.llm.error = err && err.message ? err.message : "保存失败";
        } finally {
            state.llm.busy = false;
            renderLlmCard();
        }
        // 首次填完 Key 顺手出一份解读，省得再点一次
        if (saved && !(state.payload || {}).llm_summary) await doLlmGenerate(false);
    }

    // 手动重跑日报后，后台会静默补一份解读；轻量轮询把结果贴回卡片（不整页重绘）
    async function pollLlmSummary(tries) {
        for (var i = 0; i < tries; i++) {
            await new Promise(function (r) { setTimeout(r, 5000); });
            try {
                var resp = await fetchWithAuth("/api/hotspot/today");
                var data = await resp.json();
                if (data.status === "ok" && data.payload && data.payload.llm_summary) {
                    if (state.payload) state.payload.llm_summary = data.payload.llm_summary;
                    state.llm.note = "已自动补上 AI 解读";
                    renderLlmCard();
                    return;
                }
            } catch (e) { /* 轮询失败静默，下一轮再试 */ }
        }
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
        html += '<div class="th-section"><div class="th-card th-llm" id="thLlm"></div></div>';
        html += '<div class="th-section"><div class="th-section-title">今日主线'
            + "<em>涨停股所属行业聚簇 × 当日新闻命中 · 家数 ≥3 取前 3</em>"
            + '<a class="th-trend-link" href="/hotspot-trend">看热点趋势 →</a></div>'
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
        renderLlmCard();
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
            + "<h3>还没有热点日报</h3>"
            + "<p>" + esc(message || "收盘轮（21:34）跑完会自动生成。") + "</p>"
            + '<button class="btn btn-primary" id="thEmptyGen">立即生成</button>'
            + "</div>";
        var btn = $("thEmptyGen");
        if (btn) btn.addEventListener("click", doRefresh);
    }

    // ---------------------------------------------------------------- 取数
    // 不带 date 参数：后端会返回库里最新的一份，页面永远只看最近交易日。
    async function load() {
        try {
            var resp = await fetchWithAuth("/api/hotspot/today");
            var data = await resp.json();
            notice("", "");

            if (data.status !== "ok") {
                $("thUpdated").textContent = "尚无数据";
                renderEmpty(data.message);
                return;
            }

            state.payload = data.payload;
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
                var themeNames = (s.themes || []).slice(0, THEME_LIMIT);
                notice("ok", "已生成 " + (s.market_date || "") + " 的日报：涨停 " + (s.limit_up || 0)
                    + " 家，主线 " + (themeNames.join("、") || "无")
                    + "；AI 解读稍后自动补上");
                await load();
                pollLlmSummary(6);
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
        var btn = $("thRefresh");
        if (btn) btn.addEventListener("click", doRefresh);
        load();
        loadLlmConfig();
    });
})();
