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
