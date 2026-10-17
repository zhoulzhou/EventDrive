"""启动 Web 服务（可同时承载定时任务）。

- 本地开发：直接 `python run_server.py`，默认监听 0.0.0.0:8000，**不**内嵌定时任务
  （本地如需定时抓取，另开一个终端跑 `python run_scheduler.py`，避免两处重复抓取）
- 云端部署：沙箱会注入 `PORT` 环境变量，必须监听 0.0.0.0:$PORT 才能被反向代理访问；
  且云端只能运行**一个**服务进程，无法再单独拉起调度器进程，因此默认在 Web 进程内
  启动调度器（START_SCHEDULER 缺省置为 true），使定时抓取真正常驻运行。

可用环境变量：
    PORT            监听端口（云端注入）
    START_SCHEDULER 是否在 Web 进程内启动定时任务；云端缺省 true，本地缺省沿用 .env（false）
"""
import os

import uvicorn

from app.config import settings

# 云端注入 PORT；本地未设置时回落到 .env 的 PORT（默认 8000）
port = int(os.getenv("PORT") or settings.PORT)

# 判定是否云端环境：存在 PORT 注入即视为云端
is_cloud = bool(os.getenv("PORT"))

# 调度器开关：云端缺省开启（单进程必须内嵌），且不覆盖用户显式设置
if is_cloud and "START_SCHEDULER" not in os.environ:
    os.environ["START_SCHEDULER"] = "true"
    settings.START_SCHEDULER = True

if __name__ == "__main__":
    print(f"启动新闻抓取应用服务器... 监听 0.0.0.0:{port}")
    print(
        "定时任务: "
        + ("已内嵌启动（云端模式）" if settings.START_SCHEDULER else "未启动，如需请另跑 run_scheduler.py")
    )
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )