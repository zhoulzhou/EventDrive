#!/bin/bash

# 新闻抓取应用启动脚本 (Ubuntu 22.04)
# 使用方法: ./run.sh [server|scheduler|all]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

MODE=${1:-all}

echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  新闻抓取应用启动 (模式: $MODE)${NC}"
echo -e "${YELLOW}============================================${NC}"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到 Python3${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo -e "${GREEN}✅ ${PYTHON_VERSION}${NC}"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}⚠️  创建虚拟环境...${NC}"
    python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt
echo -e "${GREEN}✅ 依赖已安装${NC}"

mkdir -p data/images logs

if [ ! -f ".env" ]; then
    cp .env.example .env
fi

run_server() {
    echo -e "${GREEN}🚀 启动 FastAPI 服务器...${NC}"
    uvicorn app.main:app --host 0.0.0.0 --port 8000
}

run_scheduler() {
    echo -e "${GREEN}🚀 启动定时任务调度器...${NC}"
    python3 run_scheduler.py
}

case $MODE in
    server)
        run_server
        ;;
    scheduler)
        run_scheduler
        ;;
    all|*)
        echo -e "${YELLOW}📦 启动定时调度器(后台)...${NC}"
        nohup python3 run_scheduler.py > logs/scheduler.log 2>&1 &
        SCHEDULER_PID=$!
        echo $SCHEDULER_PID > logs/scheduler.pid
        echo -e "${GREEN}✅ 调度器已启动 (PID: $SCHEDULER_PID)${NC}"

        sleep 2

        echo -e "${YELLOW}📦 启动 Web 服务器...${NC}"
        run_server
        ;;
esac