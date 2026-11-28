document.addEventListener('DOMContentLoaded', function() {
    console.log('应用已加载（公开访问，无登录）');

    // 高亮当前页面对应的导航 tab
    const path = window.location.pathname;
    document.querySelectorAll('.navbar-menu .nav-link').forEach(function (link) {
        const href = link.getAttribute('href');
        if (href && path === href) {
            link.classList.add('active');
        }
    });
});

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
