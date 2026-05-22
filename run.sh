#!/bin/bash

# EventDrive 完整启动脚本 (Ubuntu 22.04)
# 使用方法: ./run.sh
#
# 启动以下服务:
#   1. 新闻定时抓取调度器 (后台)
#   2. 飞书互动助手机器人 (后台)
#   3. Web 管理界面 (前台)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  EventDrive 完整启动${NC}"
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

mkdir -p logs

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠️  已创建 .env 文件，请编辑配置后重新运行${NC}"
    exit 1
fi

# 1. 新闻定时抓取调度器
echo -e "${YELLOW}� 启动新闻抓取调度器(后台)...${NC}"
nohup python3 run_scheduler.py > logs/scheduler.log 2>&1 &
SCHEDULER_PID=$!
echo $SCHEDULER_PID > logs/scheduler.pid
echo -e "${GREEN}✅ 调度器已启动 (PID: $SCHEDULER_PID)${NC}"

# 2. 飞书互动助手机器人
echo -e "${YELLOW}🤖 启动飞书互动助手(后台)...${NC}"
nohup python3 run_bot.py > logs/bot.log 2>&1 &
BOT_PID=$!
echo $BOT_PID > logs/bot.pid
echo -e "${GREEN}✅ 飞书机器人已启动 (PID: $BOT_PID)${NC}"

sleep 2

# 3. Web 管理界面
echo ""
echo -e "${YELLOW}🌐 启动 Web 管理界面...${NC}"
echo -e "访问地址: ${GREEN}http://localhost:8000${NC}"
echo -e "API 文档: ${GREEN}http://localhost:8000/docs${NC}"
echo ""
echo -e "日志文件:"
echo -e "  调度器: ${GREEN}logs/scheduler.log${NC}"
echo -e "  机器人: ${GREEN}logs/bot.log${NC}"
echo ""

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload