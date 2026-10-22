"""启动 Web 服务。

定时抓取**不在本进程内调度** —— 云沙箱会缩容回收进程，进程内 APScheduler 的
job 只在启动那一刻算出 next_run_time、无持久化 job store，定时点过后不会补跑
（表现为「/health 显示调度器在跑，但定时任务从不执行」）。

抓取改由**外部自动任务**在每天北京时间 16:00 调用
`POST /api/crawl/daily-refresh` 点火，见 app/api/crawl.py。

可用环境变量：
    PORT            监听端口（云端注入）；未设置时回落到 .env 的 PORT（默认 8000）
"""
import os

import uvicorn

from app.config import settings

# 云端注入 PORT；本地未设置时回落到 .env 的 PORT（默认 8000）
port = int(os.getenv("PORT") or settings.PORT)

if __name__ == "__main__":
    print(f"启动 EventDrive 服务... 监听 0.0.0.0:{port}")
    print("定时抓取: 由外部自动任务点火 POST /api/crawl/daily-refresh（16:00 收盘轮，不含财务）")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
