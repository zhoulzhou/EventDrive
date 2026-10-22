#!/bin/bash

# EventDrive 完整启动脚本 (Ubuntu 22.04)
# 使用方法: ./start.sh
#
# 启动以下服务:
#   1. 新闻定时抓取调度器 (后台)
#   2. Web 管理界面 (前台)
#
# 日志实时输出到终端, 方便排查问题

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cleanup() {
    echo ""
    echo -e "${GREEN}服务已停止${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  EventDrive 启动${NC}"
echo -e "${GREEN}  $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# ---------- 环境检查 ----------
echo -e "${YELLOW}[1/2] 环境检查...${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ 未找到 Python3, 请先安装${NC}"
    exit 1
fi
echo -e "       Python:  ${GREEN}$(python3 --version)${NC}"

if [ ! -d "venv" ]; then
    echo -e "       ${YELLOW}创建虚拟环境...${NC}"
    python3 -m venv venv
fi
source venv/bin/activate
echo -e "       venv:    ${GREEN}已激活${NC}"

pip install -q -r requirements.txt
echo -e "       依赖:    ${GREEN}已安装${NC}"

if [ ! -f ".env" ]; then
    echo -e "${YELLOW}       .env 不存在, 已从 .env.example 创建, 请编辑后重新运行${NC}"
    cp .env.example .env
    exit 1
fi
echo -e "       .env:    ${GREEN}已就绪${NC}"

echo -e "${GREEN}  ✓ 环境检查通过${NC}"
echo ""



# ---------- 启动 Web 服务 ----------
echo -e "${YELLOW}[2/2] 启动 Web 服务...${NC}"
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Web 服务已启动${NC}"
echo -e ""
echo -e "  Web 管理:  ${GREEN}http://localhost:8000${NC}"
echo -e "  API 文档:  ${GREEN}http://localhost:8000/docs${NC}"
echo -e ""
echo -e "  定时抓取:  ${GREEN}外部自动任务在北京时间 16:00 调用${NC}"
echo -e "             ${GREEN}POST /api/crawl/daily-refresh 点火${NC}"
echo -e "             ${GREEN}（抓取除财务外的所有页面数据）${NC}"
echo -e ""
echo -e "  ${YELLOW}Ctrl+C 停止服务${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# 检查并清理 8000 端口上的旧 Web 进程, 避免 Address already in use 导致旧代码继续服务
OLD_WEB_PIDS=$(ss -ltnp 2>/dev/null | grep ':8000 ' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)
if [ -n "$OLD_WEB_PIDS" ]; then
    echo -e "${YELLOW}      检测到 8000 端口被旧进程占用, 停止以下进程:${NC}"
    for pid in $OLD_WEB_PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            cmd=$(ps -p "$pid" -o cmd= 2>/dev/null)
            if echo "$cmd" | grep -qE 'uvicorn|app\.main|run_server'; then
                echo -e "       - PID $pid: $(echo "$cmd" | cut -c1-90)"
                kill "$pid" 2>/dev/null
                sleep 1
                if kill -0 "$pid" 2>/dev/null; then
                    kill -9 "$pid" 2>/dev/null
                    echo -e "         未正常退出, 已强制停止"
                fi
            else
                echo -e "${RED}       - PID $pid 占用 8000 端口但非本应用进程, 跳过, 请手动确认${NC}"
            fi
        fi
    done
    sleep 1
    echo -e "${GREEN}  ✓ 旧 Web 进程已清理${NC}"
fi

uvicorn app.main:app --host 127.0.0.1 --port 8000