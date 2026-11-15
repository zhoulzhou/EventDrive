document.addEventListener('DOMContentLoaded', function() {
    console.log('新闻抓取应用已加载');
    
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', handleLogout);
    }

    // 高亮当前页面对应的导航 tab
    const path = window.location.pathname;
    document.querySelectorAll('.navbar-menu .nav-link').forEach(function (link) {
        const href = link.getAttribute('href');
        if (href && path === href) {
            link.classList.add('active');
        }
    });
});

async function handleLogout() {
    try {
        await fetch('/api/logout', { method: 'POST' });
        window.location.href = '/login';
    } catch (error) {
        console.error('登出失败:', error);
        window.location.href = '/login';
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatTime(timeStr) {
    const date = new Date(timeStr);
    return date.toLocaleString('zh-CN');
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}

function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

/* 判断响应体是不是 JSON（只读 clone ，不消耗原 body） */
function _looksJson(response) {
    const ct = (response.headers.get("content-type") || "").toLowerCase();
    if (ct.indexOf("json") >= 0) return Promise.resolve(true);
    // 有些失败响应不带 content-type，读一小段内容兜底判断
    return response.clone().text().then(function (text) {
        const t = (text || "").trim();
        return t.charAt(0) === "{" || t.charAt(0) === "[";
    }).catch(function () { return false; });
}

/**
 * 带登录态与「网关抖动」重试的请求封装。
 *
 * 沙箱网关会偶发返回**纯文本 404 "Not Found"**（2026-10-25 实测：/api/macro、/health、
 * 静态文件都会命中，约 5~10%，与业务代码无关）。这类响应交给调用方的 `response.json()`
 * 会炸成 `Unexpected token 'N', "Not Found" is not valid JSON`，首屏第一次加载踩中就是
 * 「加载失败」。这里对 **GET 且非 JSON 的失败响应**做短重试（300ms / 600ms），
 * 三次都失败才抛可读错误。
 *
 * 规则刻意保守：
 * - **2xx 一律直接返回**（不碰 content-type，避免误伤 204 空响应等正常场景）
 * - **非 2xx 但 body 是 JSON** 一律直接返回 —— 那是后端业务错误（400/500 带 message），
 *   交给调用方按 data.message 展示
 * - 只有「非 2xx 且非 JSON」才当成网关抖动重试
 * - **非 GET 不重试**：POST 多为「点火后台抓取」，重放没有收益
 */
async function fetchWithAuth(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const attempts = method === "GET" ? 3 : 1;
    let lastStatus = 0;

    for (let i = 1; i <= attempts; i++) {
        let response;
        try {
            response = await fetch(url, options);
        } catch (err) {
            if (i === attempts) {
                throw new Error("网络异常（" + (err && err.message ? err.message : "请求失败") + "），请检查网络后重试");
            }
            await sleep(300 * i);
            continue;
        }

        if (response.status === 401) {
            window.location.href = '/login';
            throw new Error('未授权，已重定向到登录页');
        }

        lastStatus = response.status;
        if (response.ok) return response;
        if (await _looksJson(response)) return response;

        if (i === attempts) break;
        await sleep(300 * i);
    }

    throw new Error("服务暂时不可用（HTTP " + lastStatus + "）"
        + (attempts > 1 ? "，已自动重试仍失败，请稍后刷新页面" : "，请稍后重试"));
}

/* ==================== 页头长说明折叠 ====================
 * 自动把 .page-header 内的 .form-hint 包进 .page-intro，默认只显示两行。
 * 全局生效，新增页面无需改模板；内容不足两行时不裁切、不显示按钮。
 * 只取 .page-header 的直接子元素，所以 <details> 内、图表下方的说明不受影响。
 */
(function initPageIntro() {
    /* 渐隐区要跟页面背景同色。部分页面把页头放在白色卡片里，
       写死 #f5f5f5 会出现色块，所以往上找第一个不透明实色背景。 */
    function detectBg(el) {
        var node = el.parentElement;
        while (node && node !== document.documentElement) {
            var c = window.getComputedStyle(node).backgroundColor || '';
            var m = c.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)$/);
            if (m && (!m[4] || parseFloat(m[4]) >= 0.95)) return c;
            node = node.parentElement;
        }
        return window.getComputedStyle(document.body).backgroundColor || '#f5f5f5';
    }

    /* 量出「当前可见内容最后一行」的文字右边界（相对 wrap 左边）。
       Range 对跨行文本节点按行返回多个矩形，无需逐字符测量。
       折叠态只认第二行；展开态认全文最后一行。 */
    function lastLineRight(body, wrap, lineHeight, clamped) {
        var wrapRect = wrap.getBoundingClientRect();
        var bodyTop = body.getBoundingClientRect().top;
        var minTop = clamped ? bodyTop + lineHeight - 3 : -Infinity;
        var maxTop = clamped ? bodyTop + 2 * lineHeight - 3 : Infinity;
        var bestTop = -Infinity;
        var maxRight = wrapRect.left;
        var walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
        var node;
        while ((node = walker.nextNode())) {
            if (!node.nodeValue || !node.nodeValue.trim()) continue;
            var range = document.createRange();
            range.selectNodeContents(node);
            var rects = range.getClientRects();
            for (var i = 0; i < rects.length; i++) {
                var r = rects[i];
                if (r.top < minTop || r.top >= maxTop) continue;
                if (r.top > bestTop + 2) {
                    bestTop = r.top;
                    maxRight = r.right;
                } else if (r.top >= bestTop - 2 && r.right > maxRight) {
                    maxRight = r.right;
                }
            }
        }
        return Math.max(0, maxRight - wrapRect.left);
    }

    /* 把按钮放到该行文字末尾；剩余宽度不够时退回右端 + 渐隐遮罩。 */
    function placeBtn(wrap, body, btn, lineHeight, clamped) {
        if (btn.hidden) return;
        wrap.classList.remove('intro-fade');
        btn.style.left = '0';
        btn.style.right = 'auto';
        var wrapWidth = wrap.getBoundingClientRect().width;
        var btnWidth = btn.offsetWidth;
        var x = lastLineRight(body, wrap, lineHeight, clamped);
        if (x + btnWidth + 6 <= wrapWidth) {
            btn.style.left = (x + 6) + 'px';
        } else {
            wrap.classList.add('intro-fade');
            btn.style.left = 'auto';
            btn.style.right = '0';
        }
    }

    function setup() {
        document.querySelectorAll('.page-header').forEach(function (header) {
            var hints = Array.prototype.filter.call(header.children, function (el) {
                return el.classList && el.classList.contains('form-hint');
            });
            if (!hints.length) return;

            var wrap = document.createElement('div');
            wrap.className = 'page-intro';
            var body = document.createElement('div');
            body.className = 'intro-body';
            header.insertBefore(wrap, hints[0]);
            wrap.style.setProperty('--intro-bg', detectBg(wrap));
            hints.forEach(function (h) { body.appendChild(h); });
            wrap.appendChild(body);

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'intro-toggle';
            btn.textContent = '显示全部';
            btn.setAttribute('aria-expanded', 'false');
            btn.hidden = true;
            wrap.appendChild(btn);

            var expanded = false;
            var lineHeight = 0;
            function measure() {
                body.classList.remove('is-clamped');
                var cs = window.getComputedStyle(hints[0]);
                var fontSize = parseFloat(cs.fontSize) || 14;
                lineHeight = parseFloat(cs.lineHeight) || fontSize * 1.7;
                var twoLines = lineHeight * 2;
                body.style.setProperty('--intro-clamp', twoLines + 'px');
                var need = body.scrollHeight > twoLines + 2;
                btn.hidden = !need;
                if (need && !expanded) body.classList.add('is-clamped');
                if (!need) {
                    expanded = false;
                    wrap.classList.remove('expanded');
                    btn.textContent = '显示全部';
                    btn.setAttribute('aria-expanded', 'false');
                }
                placeBtn(wrap, body, btn, lineHeight, body.classList.contains('is-clamped'));
            }

            btn.addEventListener('click', function () {
                expanded = !expanded;
                body.classList.toggle('is-clamped', !expanded);
                wrap.classList.toggle('expanded', expanded);
                btn.textContent = expanded ? '收起' : '显示全部';
                btn.setAttribute('aria-expanded', String(expanded));
                placeBtn(wrap, body, btn, lineHeight, !expanded);
            });

            measure();
            // 动态填充的说明（如 /market 的 #metaInfo）完成后重新测量
            if (typeof MutationObserver !== 'undefined') {
                var mo = new MutationObserver(function () {
                    if (!expanded) measure();
                });
                mo.observe(body, { childList: true, subtree: true, characterData: true });
            }
            window.addEventListener('resize', function () {
                if (!expanded) measure();
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setup);
    } else {
        setup();
    }
})();
