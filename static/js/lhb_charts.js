/* 龙虎榜页面（/lhb）：
 *   - 榜单渲染 + 展开席位明细 + 买五卖五对照图（ECharts）
 *   - 席位字典 / 席位实测画像 / 后台抓取轮询
 *
 * 数据全部来自 /api/lhb*（后端只读库），阈值与文案口径见 app/utils/lhb.py 与
 * docs/龙虎榜.txt、docs/龙虎榜案例.txt。改分档颜色/文案时三处同步：
 * 本文件的 GRADE_TONE、app/utils/lhb.py 的 _grade_of、lhb.css 的 .lb-grade.*
 */

const LHB = {
    state: {
        date: null,
        dates: [],
        records: [],
        stats: {},
        scenes: [],
        filter: null,  // 初始为 null，加载完按 defaultSceneKey() 选「优质观察信号」
        updatedAt: "—",
        stale: false,
        notice: "",
        openTradeId: null,
        detailSeq: 0,
        chart: null,
        seatProfiles: {},
        types: {},
    },
};

const GRADE_TONE = {
    "重点观察池": "good",
    "跟踪观察": "info",
    "中性": "neutral",
    "风险池": "warn",
    "强风险": "danger",
};

/* ---------------- 格式化 ---------------- */

function lbMoney(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    const n = Number(v);
    const d = digits === undefined ? 2 : digits;
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(d) + "亿";
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(0) + "万";
    return n.toFixed(0);
}

function lbPct(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    const d = digits === undefined ? 1 : digits;
    return Number(v).toFixed(d) + "%";
}

function lbSignedMoney(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    const s = lbMoney(v, 2);
    return Number(v) > 0 ? "+" + s : s;
}

function lbUpDown(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return '<span class="lb-flat">—</span>';
    const n = Number(v);
    const cls = n > 0 ? "lb-up" : (n < 0 ? "lb-down" : "lb-flat");
    const sign = n > 0 ? "+" : "";
    return '<span class="' + cls + '">' + sign + n.toFixed(2) + "%</span>";
}

function lbTone(key, fallback) {
    const map = { good: "#22c55e", info: "#3b82f6", neutral: "#94a3b8", warn: "#f59e0b", danger: "#ef4444" };
    return map[key] || fallback || "#64748b";
}

function lbTag(text, tone) {
    return '<span class="lb-tag ' + (tone || "") + '">' + escapeHtml(text) + "</span>";
}

/* ---------------- 概览 ---------------- */

function renderStats() {
    const s = LHB.state.stats || {};
    const cards = [
        { label: "上榜记录 / 个股", value: (s.rows || 0) + " / " + (s.stocks || 0),
          hint: "一条记录 = 股票 × 一个上榜原因", tone: "#667eea" },
        { label: "龙虎净额合计", value: lbSignedMoney(s.net_amt),
          hint: "买五合计 − 卖五合计", tone: s.net_amt >= 0 ? "#ef4444" : "#22c55e" },
        { label: "机构净买入", value: lbSignedMoney(s.inst_net),
          hint: "机构专用席位买卖轧差", tone: s.inst_net >= 0 ? "#ef4444" : "#22c55e" },
        { label: "北向净买入", value: lbSignedMoney(s.north_net),
          hint: "沪股通 + 深股通", tone: s.north_net >= 0 ? "#ef4444" : "#22c55e" },
        { label: "观察池（≥60 分）", value: (s.observe || 0) + " 条",
          hint: "低位/合力/买盘分散", tone: "#8b5cf6" },
        { label: "机构出逃·游资接盘", value: (s.inst_out || 0) + " 条",
          hint: "卖榜机构净卖、买榜游资", tone: (s.inst_out ? "#ef4444" : "#94a3b8") },
        { label: "拉萨扎堆", value: (s.lhasa || 0) + " 条",
          hint: "散户大本营包揽买榜", tone: (s.lhasa ? "#ef4444" : "#94a3b8") },
        { label: "高位上榜", value: (s.high_position || 0) + " 条",
          hint: "默认按兑现视角解读", tone: (s.high_position ? "#f59e0b" : "#94a3b8") },
        { label: "告警条数", value: (s.alerts || 0),
          hint: "对倒/孤庄/三日榜等", tone: (s.alerts ? "#b45309" : "#94a3b8") },
    ];
    return '<div class="lb-cards">' + cards.map(function (c) {
        return '<div class="lb-card" style="border-top-color:' + c.tone + '">'
            + '<div class="lb-card-label">' + escapeHtml(c.label) + "</div>"
            + '<div class="lb-card-value" style="color:' + c.tone + '">' + c.value + "</div>"
            + '<div class="lb-card-hint">' + escapeHtml(c.hint) + "</div></div>";
    }).join("") + "</div>";
}

/* ---------------- 场景分类（docs/龙虎榜案例.txt 的「案例对照清单」） ----------------
 * 七个场景的顺序、标签、结构描述、处理策略全部由后端 engine.SCENES 下发
 * （app/utils/lhb.py），前端只负责分组与配色，避免中英文案抄两遍走样。
 */

function sceneMeta(key) {
    const list = LHB.state.scenes || [];
    for (let i = 0; i < list.length; i++) {
        if (list[i].key === key) return list[i];
    }
    return { key: key || "other", label: "其他结构", icon: "·", tone: "muted",
             tag: "", structure: "", strategy: "" };
}

function scenePill(key) {
    if (!key) return "";
    const m = sceneMeta(key);
    return '<span class="lb-scene-pill tone-' + escapeHtml(m.tone || "muted") + '">'
        + escapeHtml(m.icon || "") + " " + escapeHtml(m.label) + "</span>";
}

function applyFilter(records) {
    const f = LHB.state.filter;
    const all = records || [];
    if (!f || f === "all") return all;
    return all.filter(function (r) { return (r.scene || "other") === f; });
}

function sceneCounts() {
    return (LHB.state.stats || {}).scene_counts || {};
}

/* 进场默认落点：清单第一个场景（优质观察信号）。
 * 当天它没有记录时顺延到第一个有数据的场景 —— 否则一进页面就是空表，
 * 用户会以为 21:34 那轮没抓到数据。 */
function defaultSceneKey() {
    const list = LHB.state.scenes || [];
    const counts = sceneCounts();
    for (let i = 0; i < list.length; i++) {
        if ((counts[list[i].key] || 0) > 0) return list[i].key;
    }
    return list.length ? list[0].key : "all";
}

function renderFilters() {
    const counts = sceneCounts();
    const total = (LHB.state.records || []).length;
    // 场景按钮固定全列（清单顺序，第一个就是「优质观察信号」），0 条也标 0：
    // 「今天没有这类结构」本身是信息。真正不占位的是下面的表格分节（0 条不画空表块）。
    const chips = (LHB.state.scenes || []).map(function (s) {
        return { key: s.key, label: s.label, icon: s.icon, count: counts[s.key] || 0 };
    });
    if (!chips.length) chips.push({ key: "all", label: "全部", icon: "", count: total });
    const shown = applyFilter(LHB.state.records).length;
    return '<div class="lb-filters">'
        + chips.map(function (c) {
            return '<button class="lb-chip' + (LHB.state.filter === c.key ? " active" : "")
                + (c.count ? "" : " empty")
                + '" data-filter="' + escapeHtml(c.key) + '" title="' + escapeHtml(c.label) + '">'
                + (c.icon ? '<span class="lb-chip-icon">' + escapeHtml(c.icon) + "</span>" : "")
                + escapeHtml(c.label)
                + '<span class="lb-chip-num">' + c.count + "</span></button>";
        }).join("")
        + '<span class="lb-filter-note">当前显示 ' + shown + " / " + total + " 条</span></div>";
}

/* ---------------- 榜单表格（按场景分组） ---------------- */

const TABLE_HEAD = '<table class="lb-table"><thead><tr>'
    + "<th>股票</th><th>涨跌幅</th><th>上榜原因</th>"
    + '<th class="num">龙虎净额</th><th class="num">成交占比</th>'
    + '<th class="num">买一集中度</th><th>席位构成</th><th>位置</th>'
    + '<th class="num">分数</th><th>告警</th></tr></thead><tbody>';

function rowHtml(r) {
    const tone = GRADE_TONE[r.grade] || "neutral";
    const scopeBadge = r.scope === "3day"
        ? '<span class="lb-badge three">三日榜</span>'
        : '<span class="lb-badge day">单日榜</span>';
    const posBadge = r.position
        ? '<span class="lb-badge ' + (r.position === "高位" ? "pos-high" : (r.position === "低位" ? "pos-low" : "pos"))
          + '">' + escapeHtml(r.position) + "</span>"
        : '<span class="lb-badge pos">—</span>';
    const posTip = r.pos_pct === null || r.pos_pct === undefined
        ? "缺日线数据" : ("近20日 " + r.pos_pct + "% · 连板 " + (r.streak || 0));
    const alerts = (r.alerts || []);
    const alertHtml = alerts.length
        ? alerts.slice(0, 3).map(function (a) {
            return lbTag(a.tag, a.level === "danger" ? "danger" : (a.level === "warn" ? "warn" : "info"));
        }).join("") + (alerts.length > 3 ? '<span class="lb-alert-dots">+' + (alerts.length - 3) + "</span>" : "")
        : '<span class="lb-flat" style="font-size:0.74rem">—</span>';
    const tagTone = (r.combo_tag || "").indexOf("出逃") >= 0 || r.combo_tag === "拉萨扎堆"
        ? "danger" : (r.combo_tag === "机构+游资合力" ? "good" : "");
    const instLine = (r.inst_net === null || r.inst_net === undefined || !r.inst_net) ? ""
        : '<div class="lb-card-hint">机构 ' + lbSignedMoney(r.inst_net) + "</div>";

    return '<tr class="lb-row' + (LHB.state.openTradeId === r.trade_id ? " open" : "")
        + '" data-tid="' + escapeHtml(r.trade_id) + '">'
        + "<td><span class=\"lb-name\">" + escapeHtml(r.name) + "</span>"
        + '<span class="lb-code">' + escapeHtml(r.code) + "</span></td>"
        + "<td>" + lbUpDown(r.change_rate) + "</td>"
        + '<td><div class="lb-cause">' + scopeBadge + " " + escapeHtml(r.explanation || "") + "</div></td>"
        + '<td class="num"><span style="font-weight:' + (r.net_amt >= 0 ? 600 : 500) + ';color:'
        + (r.net_amt >= 0 ? "#ef4444" : "#22c55e") + '">' + lbSignedMoney(r.net_amt) + "</span>"
        + instLine + "</td>"
        + '<td class="num">' + lbPct(r.deal_ratio) + "</td>"
        + '<td class="num">' + lbPct(r.buy1_ratio, 0) + "</td>"
        + "<td>" + lbTag(r.combo_tag || "—", tagTone) + "</td>"
        + '<td title="' + escapeHtml(posTip) + '">' + posBadge + "</td>"
        + '<td class="num"><span class="lb-score" style="color:' + lbTone(tone) + '">'
        + (r.score === null ? "—" : r.score)
        + '</span><div><span class="lb-grade ' + tone + '">' + escapeHtml(r.grade || "") + "</span></div></td>"
        + "<td>" + alertHtml + "</td>"
        + "</tr>";
}

function sceneBlock(meta, rows) {
    const tone = meta.tone || "muted";
    return '<section class="lb-scene tone-' + escapeHtml(tone) + '" data-scene="' + escapeHtml(meta.key) + '">'
        + '<div class="lb-scene-head">'
        + '<div class="lb-scene-title">'
        + '<span class="lb-scene-icon">' + escapeHtml(meta.icon || "·") + "</span>"
        + escapeHtml(meta.label)
        + '<span class="lb-scene-count">' + rows.length + " 条</span>"
        + (meta.tag ? '<span class="lb-scene-tag">' + escapeHtml(meta.tag) + "</span>" : "")
        + "</div>"
        + (meta.structure ? '<div class="lb-scene-row">结构：' + escapeHtml(meta.structure) + "</div>" : "")
        + (meta.strategy ? '<div class="lb-scene-row strategy">处理策略：' + escapeHtml(meta.strategy) + "</div>" : "")
        + "</div>"
        + '<div class="lb-table-wrap">' + TABLE_HEAD + rows.map(rowHtml).join("") + "</tbody></table></div>"
        + "</section>";
}

function renderTable() {
    const rows = applyFilter(LHB.state.records);
    if (!rows.length) {
        const f = LHB.state.filter;
        const m = f && f !== "all" ? sceneMeta(f) : null;
        return '<div class="lb-table-wrap"><div class="lb-empty">'
            + (m ? "「" + escapeHtml(m.label) + "」今日没有记录" : "这个交易日没有可展示的记录")
            + (m && m.structure ? '<div class="lb-empty-sub">结构：' + escapeHtml(m.structure) + "</div>" : "")
            + (m && m.strategy ? '<div class="lb-empty-sub">处理策略：' + escapeHtml(m.strategy) + "</div>" : "")
            + "</div></div>";
    }
    const groups = {};
    rows.forEach(function (r) {
        const k = r.scene || "other";
        (groups[k] = groups[k] || []).push(r);
    });
    // 场景顺序以后端下发的清单为准（= 文档顺序）；出现未知 key 时补在末尾
    const order = (LHB.state.scenes || []).map(function (s) { return s.key; })
        .filter(function (k) { return groups[k] && groups[k].length; });
    Object.keys(groups).forEach(function (k) {
        if (order.indexOf(k) < 0) order.push(k);
    });
    return order.map(function (k) { return sceneBlock(sceneMeta(k), groups[k]); }).join("");
}

/* ---------------- 席位明细 ---------------- */

function seatRows(seats, flag) {
    if (!seats.length) {
        return '<div class="lb-flat" style="font-size:0.76rem;padding:0.3rem 0">暂无席位明细（抓取时未取到）</div>';
    }
    return seats.map(function (s) {
        const meta = (LHB.state.types[s.seat_type] || {});
        const tone = meta.tone || s.seat_type_tone || "#94a3b8";
        const alias = s.alias ? '<span class="lb-seat-alias">' + escapeHtml(s.alias) + "</span>" : "";
        const amount = flag === "buy"
            ? '<div class="lb-seat-amt buy">' + lbMoney(s.buy) + "</div>"
            : '<div class="lb-seat-amt sell">' + lbMoney(s.sell) + "</div>";
        const other = flag === "buy"
            ? '<div class="lb-seat-amt" style="color:#94a3b8">' + (s.sell ? lbMoney(s.sell) : "—") + "</div>"
            : '<div class="lb-seat-amt" style="color:#94a3b8">' + (s.buy ? lbMoney(s.buy) : "—") + "</div>";
        // 「机构专用」席位的名字和类型标签是同一个词，重复显示两遍只是噪声
        const typeBadge = (s.seat_type_label && s.seat_type_label !== s.dept_name)
            ? '<span class="lb-seat-type" style="background:' + tone + '1f;color:' + tone + '">'
              + escapeHtml(s.seat_type_label) + "</span>"
            : "";
        return '<div class="lb-seat-row" data-dept="' + escapeHtml(s.dept_name) + '">'
            + '<div class="lb-seat-rank">' + s.seq + "</div>"
            + '<div class="lb-seat-name" title="' + escapeHtml(s.dept_name) + '">'
            + escapeHtml(s.dept_name) + alias + typeBadge + "</div>"
            + amount + other + "</div>";
    }).join("");
}

/* ---------------- 明细页零件（指标卡 / 分数条 / 因子拆解） ----------------
 * 布局按设计稿：标题 + 涨跌幅标签 → 0-100 分数渐进条 → 四个关键指标卡
 * → 打分因子拆解（每项一条进度条 + 一句解释）→ 席位明细 / 买五卖五图 / 历史上榜。
 * 颜色语义两套并存：涨跌幅沿用 A 股习惯（红涨绿跌），得分高低用「好=绿」，
 * 与列表页分数列、档位徽标保持一致。
 */

// 金额拆成「数值 + 单位」，卡片里数值大字、单位小字。
// 千万以上一律用「亿」（0.51 亿），和设计稿的「买五 0.74 · 卖五 0.85」同一口径；
// 只有不到千万的才退回「万」，否则会出现「0.05 亿」这种读不出来的数。
function lbAmountParts(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return { n: "—", u: "" };
    const n = Number(v);
    const a = Math.abs(n);
    if (a >= 1e7) return { n: (n / 1e8).toFixed(2), u: "亿" };
    if (a >= 1e4) return { n: (n / 1e4).toFixed(0), u: "万" };
    return { n: String(Math.round(n)), u: "" };
}

function lbMarketShort(code, market) {
    const c = String(code || "");
    if (/^68/.test(c)) return "科创板";
    if (/^30/.test(c)) return "创业板";
    if (/^60/.test(c)) return "沪主板";
    if (/^00/.test(c)) return "深主板";
    if (/^(4|8|92)/.test(c)) return "北交所";
    return market || "—";
}

function lbComboTone(tag) {
    if (!tag) return "#94a3b8";
    if (tag.indexOf("出逃") >= 0 || tag === "拉萨扎堆") return "#ef4444";
    if (tag === "机构+游资合力") return "#16a34a";
    if (tag.indexOf("机构") >= 0) return "#2563eb";
    return "#475569";
}

function heroBlock(r) {
    const pct = (r.change_rate === null || r.change_rate === undefined) ? null : Number(r.change_rate);
    const pctTone = pct === null ? "flat" : (pct > 0 ? "up" : (pct < 0 ? "down" : "flat"));
    const pctText = pct === null ? "—" : (pct > 0 ? "+" : "") + pct.toFixed(2) + "%";
    const scopeText = r.scope === "3day" ? "三日榜" : "单日榜";
    const posText = r.position ? r.position : "位置未知";
    const posDesc = (r.pos_pct === null || r.pos_pct === undefined)
        ? "" : ("近 20 日 " + r.pos_pct + "%" + (r.streak ? " · " + r.streak + " 连板" : ""));

    const tags = [
        '<span class="lb-hero-pct ' + pctTone + '">' + escapeHtml(pctText) + "</span>",
        '<span class="lb-hero-tag">' + escapeHtml(posText) + "</span>",
        '<span class="lb-hero-tag">' + escapeHtml(lbMarketShort(r.code, r.market)) + "</span>",
        '<span class="lb-hero-tag">' + escapeHtml(scopeText) + "</span>",
    ];
    if (posDesc) tags.push('<span class="lb-hero-tag muted">' + escapeHtml(posDesc) + "</span>");
    if (r.inst_streak >= 2) {
        tags.push('<span class="lb-hero-tag streak">机构连买 ' + r.inst_streak + " 日</span>");
    }

    const score = (r.score === null || r.score === undefined) ? null : Number(r.score);
    const tone = lbTone(GRADE_TONE[r.grade] || "neutral");
    const line = score === null ? "" : '<div class="lb-scoreline">'
        + '<div class="lb-scorebar"><i class="lb-scorepin" style="left:'
        + Math.max(0, Math.min(100, score)) + "%;background:" + tone + '"></i></div>'
        + '<div class="lb-scoremeta"><span>0 · 强风险</span>'
        + '<span class="lb-scoreval" style="color:' + tone + '">'
        + escapeHtml(r.grade || "") + " · " + score + " 分</span>"
        + "<span>重点观察池 · 100</span></div></div>";

    const note = [
        r.explanation ? "上榜原因：" + r.explanation : "",
        "收盘 " + (r.close === null || r.close === undefined ? "—" : Number(r.close).toFixed(2)),
        "换手 " + lbPct(r.turnover_rate),
        "成交额 " + lbMoney(r.accum_amount, 1),
    ].filter(Boolean).join(" · ");

    return '<header class="lb-hero">'
        + '<div class="lb-hero-top">'
        + '<span class="lb-hero-name">' + escapeHtml(r.name) + "</span>"
        + '<span class="lb-hero-code">' + escapeHtml(r.code) + "</span>"
        + scenePill(r.scene)
        + "</div>"
        + '<div class="lb-hero-tags">' + tags.join("") + "</div>"
        + line
        + '<div class="lb-hero-note">' + escapeHtml(note) + "</div>"
        + "</header>";
}

function mcardHtml(c) {
    return '<div class="lb-mcard">'
        + '<div class="lb-mcard-label">' + escapeHtml(c.label) + "</div>"
        + '<div class="lb-mcard-value' + (c.text ? " text" : "") + '" style="color:' + c.tone + '">'
        + escapeHtml(c.value)
        + (c.unit ? "<small>" + escapeHtml(c.unit) + "</small>" : "")
        + "</div>"
        + '<div class="lb-mcard-sub"' + (c.subTone ? ' style="color:' + c.subTone + '"' : "") + ">"
        + escapeHtml(c.sub) + "</div></div>";
}

function mcardsBlock(r, d) {
    const net = lbAmountParts(r.net_amt);
    const b5 = lbAmountParts(r.buy_amt);
    const s5 = lbAmountParts(r.sell_amt);
    const amt = lbAmountParts(r.accum_amount);
    const first = (d.buy || [])[0];
    const buy1 = first ? lbAmountParts(first.buy) : { n: "—", u: "" };
    const alertN = (r.alerts || []).length;
    const cards = [
        {
            label: "龙虎净额", value: net.n, unit: net.u,
            tone: (Number(r.net_amt) || 0) >= 0 ? "#ef4444" : "#22c55e",
            sub: "买五 " + b5.n + b5.u + " · 卖五 " + s5.n + s5.u,
        },
        {
            label: "龙虎成交占比",
            value: r.deal_ratio === null || r.deal_ratio === undefined ? "—" : Number(r.deal_ratio).toFixed(1),
            unit: r.deal_ratio === null || r.deal_ratio === undefined ? "" : "%",
            tone: "#1f2937", sub: "占当日成交额 " + amt.n + amt.u,
        },
        {
            label: "买一集中度",
            value: r.buy1_ratio === null || r.buy1_ratio === undefined ? "—" : Number(r.buy1_ratio).toFixed(0),
            unit: r.buy1_ratio === null || r.buy1_ratio === undefined ? "" : "%",
            tone: "#1f2937", sub: "买一 " + buy1.n + buy1.u,
        },
        {
            label: "席位结构", value: r.combo_tag || "—", text: true,
            tone: lbComboTone(r.combo_tag),
            sub: "组合标签 · " + (alertN ? alertN + " 条告警" : "0 条告警"),
            subTone: alertN ? "#b45309" : "",
        },
    ];
    return '<div class="lb-mcards">' + cards.map(mcardHtml).join("") + "</div>";
}

// 单因子进度条按 |delta| / 20 归一（20 分≈一个最强单项，位置低位 10 分拍一半，和设计稿一致）
const FACTOR_BAR_MAX = 20;

function factorRow(x) {
    const delta = Number(x.delta) || 0;
    const tone = delta > 0 ? "plus" : (delta < 0 ? "minus" : "zero");
    const width = Math.min(Math.abs(delta) / FACTOR_BAR_MAX, 1) * 100;
    const label = x.label || "";
    const value = x.value || "";
    const note = x.note || x.text || "";
    return '<div class="lb-factor ' + tone + '">'
        + '<div class="lb-factor-head">'
        + '<span class="lb-factor-name">' + escapeHtml(label || "调整项")
        + (value ? '<span class="lb-factor-value"> · ' + escapeHtml(value) + "</span>" : "")
        + "</span>"
        + '<span class="lb-factor-delta">' + (delta > 0 ? "+" : "") + delta + "</span>"
        + "</div>"
        + '<div class="lb-factor-bar"><i style="width:' + width.toFixed(0) + '%"></i></div>'
        + '<div class="lb-factor-note">' + escapeHtml(note) + "</div>"
        + "</div>";
}

function factorsBlock(r) {
    const list = r.reasons || [];
    if (!list.length) {
        return '<section class="lb-factors"><div class="lb-sec-title">打分因子拆解</div>'
            + '<div class="lb-flat" style="font-size:0.78rem">这条记录没有加减分明细</div></section>';
    }
    return '<section class="lb-factors">'
        + '<div class="lb-sec-title">打分因子拆解'
        + '<span class="lb-sec-meta">基准 50 分 '
        + (r.score === null || r.score === undefined ? "" : "→ 最终 " + r.score + " 分") + "</span></div>"
        + list.map(factorRow).join("")
        + "</section>";
}

function detailHtml(d) {
    const r = d.record;
    const alerts = (r.alerts || []).map(function (a) {
        return '<li class="' + escapeHtml(a.level) + '"><b>' + escapeHtml(a.tag) + "</b>"
            + escapeHtml(a.text) + "</li>";
    }).join("");
    const hist = (d.history || []).map(function (h) {
        return "<tr><td>" + escapeHtml(h.trade_date || "") + "</td>"
            + "<td>" + (h.scope === "3day" ? "三日" : "单日") + "</td>"
            + "<td>" + lbUpDown(h.change_rate) + "</td>"
            + "<td>" + lbSignedMoney(h.net_amt) + "</td>"
            + "<td>" + lbPct(h.deal_ratio) + "</td>"
            + "<td>" + (h.score === null || h.score === undefined ? "—" : h.score) + "</td>"
            + "<td>" + escapeHtml(h.combo_tag || "—") + "</td></tr>";
    }).join("");

    return '<div class="lb-detail">'
        + heroBlock(r)
        + mcardsBlock(r, d)
        + factorsBlock(r)
        + '<div class="lb-detail-grid">'
        /* 左：席位 */
        + '<div class="lb-panel"><div class="lb-panel-title">买卖前五席位'
        + '<span class="lb-panel-sub">点营业部可查本系统实测画像</span></div>'
        + '<div class="lb-seat-block"><div class="lb-seat-head">买榜（买入金额 / 卖出金额）</div>'
        + seatRows(d.buy, "buy") + "</div>"
        + '<div class="lb-seat-block"><div class="lb-seat-head">卖榜（卖出金额 / 买入金额）</div>'
        + seatRows(d.sell, "sell") + "</div>"
        + '<div id="lbSeatProfile" class="lb-seat-profile" style="display:none"></div>'
        + "</div>"
        /* 右：图 + 组合解读 + 告警 */
        + '<div class="lb-panel"><div class="lb-panel-title">买五卖五对照'
        + '<span class="lb-panel-sub">单位：元（悬停看营业部）</span></div>'
        + '<div id="lbChart" class="lb-chart"></div>'
        + (r.combo_note
            ? '<div class="lb-panel-title" style="margin-top:0.8rem">组合解读'
              + '<span class="lb-panel-sub">' + escapeHtml(r.combo_tag || "") + "</span></div>"
              + '<div class="lb-panel-note">' + escapeHtml(r.combo_note) + "</div>" : "")
        + '<div class="lb-panel-title" style="margin-top:0.7rem">告警 '
        + '<span class="lb-panel-sub">' + (alerts ? (r.alerts || []).length + " 条" : "无") + "</span></div>"
        + (alerts ? '<ul class="lb-alert-list">' + alerts + "</ul>"
            : '<div class="lb-panel-note">没有触发对倒 / 孤庄 / 三日榜等告警</div>')
        + "</div></div>"
        /* 下：该股历史上榜 */
        + '<div class="lb-panel" style="margin-top:0.8rem"><div class="lb-panel-title">该股历史龙虎'
        + '<span class="lb-panel-sub">时序跟踪：连续多日上榜才是高价值信号</span></div>'
        + (hist
            ? '<div class="lb-history-wrap"><table class="lb-history"><thead><tr><th>日期</th><th>类型</th><th>涨跌幅</th>'
              + "<th>龙虎净额</th><th>占比</th><th>分数</th><th>席位构成</th></tr></thead><tbody>"
              + hist + "</tbody></table></div>"
            : '<div class="lb-flat" style="font-size:0.78rem">库里暂无该股的其它上榜记录（自启用起累积）</div>')
        + "</div></div>";
}

/* ---------------- 图表 ---------------- */

function drawChart(d) {
    const el = document.getElementById("lbChart");
    if (!el) return;
    if (typeof echarts === "undefined") {
        el.innerHTML = '<div class="chart-load-failed">图表库未加载，金额见左侧席位列表</div>';
        return;
    }
    if (LHB.state.chart) {
        try { LHB.state.chart.dispose(); } catch (e) { /* ignore */ }
        LHB.state.chart = null;
    }
    const items = [];
    (d.buy || []).forEach(function (s) {
        items.push({ label: "买" + s.seq, value: s.buy, dept: s.dept_name, kind: "buy" });
    });
    (d.sell || []).forEach(function (s) {
        items.push({ label: "卖" + s.seq, value: s.sell, dept: s.dept_name, kind: "sell" });
    });
    // 从下往上画：卖五在最下，买一在最上
    items.reverse();

    const chart = echarts.init(el);
    chart.setOption({
        backgroundColor: "transparent",
        grid: { left: 44, right: 78, top: 8, bottom: 8 },
        tooltip: {
            trigger: "item",
            backgroundColor: "rgba(255,255,255,0.98)",
            borderColor: "#e2e8f0",
            textStyle: { color: "#334155", fontSize: 12 },
            formatter: function (p) {
                const it = items[p.dataIndex];
                return it.label + "　" + it.dept + "<br/>金额：" + lbMoney(it.value, 2)
                    + "（" + (it.kind === "buy" ? "买入" : "卖出") + "）";
            },
        },
        xAxis: {
            type: "value",
            axisLabel: { color: "#94a3b8", fontSize: 10, formatter: function (v) { return lbMoney(v, 0); } },
            splitLine: { lineStyle: { color: "#f1f5f9" } },
            axisLine: { show: false }, axisTick: { show: false },
        },
        yAxis: {
            type: "category",
            data: items.map(function (i) { return i.label; }),
            axisLabel: { color: "#64748b", fontSize: 11 },
            axisLine: { show: false }, axisTick: { show: false },
        },
        series: [{
            type: "bar",
            barWidth: "62%",
            data: items.map(function (i) {
                return {
                    value: i.value || 0,
                    itemStyle: {
                        color: i.kind === "buy" ? "#ef4444" : "#22c55e",
                        borderRadius: [0, 3, 3, 0],
                    },
                };
            }),
            label: {
                show: true, position: "right", color: "#64748b", fontSize: 10,
                formatter: function (p) { return lbMoney(p.value, 2); },
            },
        }],
    });
    LHB.state.chart = chart;
}

/* ---------------- 详情抽屉（右侧整体展示） ----------------
 * 行内展开已废弃：表格 10 列，行内展开会把后面几十行顶到很远，也没法左右对比两条记录。
 * 现在点击行 = 右侧滑出整屏抽屉，列表位置不动。
 */

function openDrawer() {
    const wrap = document.getElementById("lbDrawer");
    if (!wrap) return;
    // 先摘掉上一次内容切换留下的 lb-swap，否则它优先级更高会顶掉滑入时内容的淡入动画
    const body = document.getElementById("lbDrawerBody");
    if (body) body.classList.remove("lb-swap");
    wrap.classList.add("open");
    wrap.setAttribute("aria-hidden", "false");
    // 抽屉盖住整屏，锁住底页滚动，避免滚轮滚到底下的表格
    document.body.style.overflow = "hidden";
}

// 内容替换后给一次极短淡入。先摘类 + 强制回流再挂上：连点两条记录时也会重新播，
// 否则第二次只是把 innerHTML 换掉，看上去像「闪了一下」。
function swapFade(el) {
    if (!el) return;
    el.classList.remove("lb-swap");
    void el.offsetWidth;
    el.classList.add("lb-swap");
}

function closeDrawer(keepHash) {
    const wrap = document.getElementById("lbDrawer");
    if (!wrap) return;
    wrap.classList.remove("open");
    wrap.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    if (LHB.state.chart) {
        try { LHB.state.chart.dispose(); } catch (e) { /* ignore */ }
        LHB.state.chart = null;
    }
    document.querySelectorAll("tr.lb-row.open").forEach(function (el) { el.classList.remove("open"); });
    LHB.state.openTradeId = null;
    LHB.state.detailSeq += 1;  // 让还在飞的请求作废
    if (!keepHash && history.replaceState) {
        history.replaceState(null, "", location.pathname + location.search);
    }
}

async function openDetail(tid) {
    const body = document.getElementById("lbDrawerBody");
    if (!body) return;
    if (LHB.state.openTradeId === tid) { closeDrawer(); return; }  // 再点同一行 = 收起

    LHB.state.openTradeId = tid;
    document.querySelectorAll("tr.lb-row.open").forEach(function (el) { el.classList.remove("open"); });
    const row = document.querySelector('tr.lb-row[data-tid="' + CSS.escape(tid) + '"]');
    if (row) row.classList.add("open");
    // 展开状态写进 URL hash：刷新/分享能直接定位到这条记录
    if (history.replaceState) history.replaceState(null, "", "#tid=" + encodeURIComponent(tid));

    const seq = ++LHB.state.detailSeq;
    body.innerHTML = '<div class="loading">正在加载席位明细…</div>';
    document.getElementById("lbDrawerTitle").textContent = "席位明细";
    body.scrollTop = 0;
    openDrawer();  // 先开抽屉再拉数据：动画期间就有反馈，不用干等接口

    try {
        const url = "/api/lhb/detail?date=" + encodeURIComponent(LHB.state.date)
            + "&trade_id=" + encodeURIComponent(tid);
        const resp = await fetchWithAuth(url, { headers: { Accept: "application/json" } });
        const data = await resp.json();
        if (data.status !== "ok") throw new Error(data.message || "加载失败");
        if (seq !== LHB.state.detailSeq) return;  // 期间点了别的行 / 关了抽屉
        body.innerHTML = detailHtml(data);
        swapFade(body);
        // 顶栏只留「席位明细」：股票名/代码在内容顶部已经是大字，重复一遍反而挤
        document.getElementById("lbDrawerTitle").textContent = "席位明细";
        drawChart(data);
        // 席位点击 → 实测画像
        body.querySelectorAll(".lb-seat-row").forEach(function (el) {
            el.addEventListener("click", function () {
                showSeatProfile(el.getAttribute("data-dept"));
            });
        });
    } catch (err) {
        if (seq !== LHB.state.detailSeq) return;
        body.innerHTML = '<div class="lb-empty">席位明细加载失败：'
            + escapeHtml(err.message || String(err))
            + '<div><button id="lbRetryDetail" class="btn btn-primary">重试</button></div></div>';
        swapFade(body);
        const b = document.getElementById("lbRetryDetail");
        if (b) b.addEventListener("click", function () { openDetail(tid); });
    }
}

async function showSeatProfile(dept) {
    const box = document.getElementById("lbSeatProfile");
    if (!box) return;
    box.style.display = "block";
    box.innerHTML = "正在查询该营业部的实测统计…";
    try {
        const resp = await fetchWithAuth("/api/lhb/seat?dept_name=" + encodeURIComponent(dept), 
            { headers: { Accept: "application/json" } });
        const d = await resp.json();
        if (d.status !== "ok") throw new Error(d.message || "查询失败");
        if (LHB.state.seatProfiles[dept]) { /* 缓存命中，直接覆盖 */ }
        LHB.state.seatProfiles[dept] = d;
        const st = d.stats || {};
        const styles = (d.styles || []).map(function (s) { return lbTag(s); }).join("");
        const nextRatio = st.next_day_sell_ratio === null || st.next_day_sell_ratio === undefined
            ? "样本不足" : st.next_day_sell_ratio + "%";
        box.innerHTML = '<div><b>' + escapeHtml(d.dept_name) + "</b>"
            + (d.alias ? '　<span class="lb-seat-alias">' + escapeHtml(d.alias) + "</span>" : "")
            + "　" + escapeHtml(d.type_label) + "</div>"
            + '<div style="margin-top:0.25rem">'
            + '<span class="lb-kv"><i>近' + d.window_days + '日上榜</i>' + (st.days || 0) + " 天</span>"
            + '<span class="lb-kv"><i>买入上榜</i>' + (st.buy_appear || 0) + " 次</span>"
            + '<span class="lb-kv"><i>隔日兑现率</i>' + nextRatio + "</span></div>"
            + (styles ? '<div style="margin-top:0.25rem">初始标签：' + styles + "</div>" : "")
            + '<div class="lb-flat" style="margin-top:0.3rem;font-size:0.72rem">' + escapeHtml(d.note || "") + "</div>";
    } catch (err) {
        box.innerHTML = "查询失败：" + escapeHtml(err.message || String(err));
    }
}

/* ---------------- 加载 / 交互 ---------------- */

function render() {
    const root = document.getElementById("lbRoot");
    const notice = document.getElementById("lbNotice");
    const s = LHB.state;
    if (!s.records || !s.records.length) {
        root.innerHTML = '<div class="lb-table-wrap"><div class="lb-empty">'
            + escapeHtml(s.notice || "这个交易日没有数据")
            + '<div><button id="lbRefresh2" class="btn btn-primary">抓取最新交易日</button></div>'
            + "</div></div>";
        const b = document.getElementById("lbRefresh2");
        if (b) b.addEventListener("click", doRefresh);
        return;
    }
    if (s.notice) {
        notice.innerHTML = '<div class="lb-notice">' + escapeHtml(s.notice) + "</div>";
    } else {
        notice.innerHTML = "";
    }
    root.innerHTML = renderStats() + renderFilters() + renderTable();
    root.querySelectorAll(".lb-chip").forEach(function (el) {
        el.addEventListener("click", function () {
            LHB.state.filter = el.getAttribute("data-filter");
            closeDrawer(true);  // 筛选后列表变了，抽屉里的记录可能已不在列表中
            render();
        });
    });
    root.querySelectorAll("tr.lb-row").forEach(function (el) {
        el.addEventListener("click", function () { openDetail(el.getAttribute("data-tid")); });
    });
    // 首次渲染后按 #tid=… 自动打开（刷新/分享保留展开状态）
    const m = /#tid=([^&]+)/.exec(location.hash || "");
    if (m && LHB.state.autoOpen !== m[1]) {
        LHB.state.autoOpen = m[1];
        const tid = decodeURIComponent(m[1]);
        const rec = LHB.state.records.filter(function (r) { return r.trade_id === tid; })[0];
        if (rec) {
            // 深链进来的记录可能不在默认场景下，先切到它所属场景再开抽屉，
            // 否则抽屉里是这条、背后列表里找不到对应的行。
            const key = rec.scene || "other";
            if (LHB.state.filter !== key && LHB.state.filter !== "all") {
                LHB.state.filter = key;
                render();
            }
            openDetail(tid);
        }
    }
}

async function loadList(date) {
    const root = document.getElementById("lbRoot");
    closeDrawer(true);  // 换日期 / 重新抓取后旧明细已失效
    root.innerHTML = '<div class="loading">正在加载龙虎榜…</div>';
    const url = "/api/lhb" + (date ? "?date=" + encodeURIComponent(date) : "");
    try {
        const resp = await fetchWithAuth(url, { headers: { Accept: "application/json" } });
        const data = await resp.json();
        if (data.status !== "ok") throw new Error(data.message || "加载失败");
        LHB.state.date = data.date;
        LHB.state.dates = data.dates || [];
        LHB.state.records = data.records || [];
        LHB.state.scenes = data.scenes || [];
        LHB.state.stats = data.stats || {};
        LHB.state.updatedAt = data.updated_at || "—";
        LHB.state.stale = !!data.stale;
        LHB.state.notice = data.note || "";
        LHB.state.openTradeId = null;
        // 首次加载 / 刷新后：当前停留的场景在新数据里没有记录了，就回到默认场景，
        // 免得刷新完停在一张空表上还以为抓取失败。
        const counts = (LHB.state.stats || {}).scene_counts || {};
        if (!LHB.state.filter || (LHB.state.filter !== "all" && !(counts[LHB.state.filter] > 0))) {
            LHB.state.filter = defaultSceneKey();
        }
        renderDateText();
        renderUpdated();
        render();
    } catch (err) {
        root.innerHTML = '<div class="lb-empty">加载失败：' + escapeHtml(err.message || String(err))
            + '<div><button id="lbRetry" class="btn btn-primary">重试</button></div></div>';
        const b = document.getElementById("lbRetry");
        if (b) b.addEventListener("click", function () { loadList(LHB.state.date); });
    }
}

function renderDateText() {
    const el = document.getElementById("lbDate");
    el.textContent = LHB.state.date ? "交易日 " + LHB.state.date : "暂无数据";
}

function renderUpdated() {
    const el = document.getElementById("lbUpdated");
    el.textContent = "数据更新于 " + LHB.state.updatedAt;
    el.className = "lb-updated" + (LHB.state.stale ? " stale" : "");
    if (LHB.state.stale) el.textContent += "（超过 26 小时未落库）";
}

async function doRefresh() {
    const btn = document.getElementById("lbRefresh");
    btn.disabled = true;
    btn.textContent = "抓取中…";
    try {
        const resp = await fetchWithAuth("/api/lhb/refresh?days=1", { method: "POST" });
        const data = await resp.json();
        if (data.status === "running") {
            btn.textContent = "已有任务在跑";
        }
        await pollRefresh(btn);
    } catch (err) {
        btn.disabled = false;
        btn.textContent = "抓取最新";
        alert("抓取失败：" + (err.message || String(err)));
    }
}

async function pollRefresh(btn) {
    for (let i = 0; i < 40; i++) {
        await sleep(3000);
        try {
            const resp = await fetchWithAuth("/api/lhb/refresh-status", { headers: { Accept: "application/json" } });
            const st = await resp.json();
            if (st.running) {
                btn.textContent = "抓取中…" + ((i + 1) * 3) + "s";
                continue;
            }
            btn.disabled = false;
            btn.textContent = "抓取最新";
            if (st.error) {
                alert("抓取失败：" + st.error);
            }
            await loadList(null);
            return;
        } catch (e) {
            /* 轮询失败就继续等下一次 */
        }
    }
    btn.disabled = false;
    btn.textContent = "抓取最新";
}

async function loadSeatDict() {
    try {
        const resp = await fetchWithAuth("/api/lhb/seat-dict", { headers: { Accept: "application/json" } });
        const data = await resp.json();
        if (data.status !== "ok") return;
        (data.types || []).forEach(function (t) { LHB.state.types[t.key] = t; });
        const typeHtml = '<div class="lb-dict-grid">' + (data.types || []).map(function (t) {
            return '<div class="lb-dict-item" style="border-left:3px solid ' + t.tone + '">'
                + '<div class="lb-dict-name">' + escapeHtml(t.label) + "</div>"
                + '<div class="lb-dict-styles">' + escapeHtml(t.note) + "</div></div>";
        }).join("") + "</div>";
        document.getElementById("lbDictTypes").innerHTML = typeHtml;
        document.getElementById("lbDictDepots").innerHTML = (data.depots || []).map(function (d) {
            return '<div class="lb-dict-item"><div class="lb-dict-name">' + escapeHtml(d.name)
                + '<span>' + escapeHtml((d.aliases || []).join("·")) + "</span></div>"
                + '<div class="lb-dict-styles">' + escapeHtml((d.styles || []).join(" · "))
                + "</div></div>";
        }).join("");
    } catch (e) {
        document.getElementById("lbDictDepots").innerHTML =
            '<div class="lb-flat">席位字典加载失败（不影响榜单）</div>';
    }
}

document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("lbRefresh").addEventListener("click", doRefresh);
    // 抽屉关闭：叉号 / 点遮罩 / Esc
    const closeBtn = document.getElementById("lbDrawerClose");
    if (closeBtn) closeBtn.addEventListener("click", function () { closeDrawer(); });
    const mask = document.querySelector("#lbDrawer .lb-drawer-mask");
    if (mask) mask.addEventListener("click", function () { closeDrawer(); });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && LHB.state.openTradeId) closeDrawer();
    });
    // 全局只挂一次 resize：抽屉里的图跟着面板宽度走（每开一次就挂一次会累积监听器）
    window.addEventListener("resize", function () {
        if (LHB.state.chart) LHB.state.chart.resize();
    });
    loadSeatDict();
    loadList(null);
});
