#!/bin/bash

# EventDrive 完整启动脚本 (Ubuntu 22.04)
# 使用方法: ./start.sh
#
# 启动以下服务:
#   1. 新闻定时抓取调度器 (后台)
#   2. 飞书互动助手机器人 (后台)
#   3. Web 管理界面 (前台)
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
    echo -e "${YELLOW}正在停止所有服务...${NC}"
    if [ -n "$SCHEDULER_PID" ] && kill -0 "$SCHEDULER_PID" 2>/dev/null; then
        kill "$SCHEDULER_PID" 2>/dev/null
        echo -e "${GREEN}✅ 已停止调度器 (PID: $SCHEDULER_PID)${NC}"
    fi
    if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
        kill "$BOT_PID" 2>/dev/null
        echo -e "${GREEN}✅ 已停止飞书机器人 (PID: $BOT_PID)${NC}"
    fi
    echo -e "${GREEN}所有服务已停止${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  EventDrive 完整启动${NC}"
echo -e "${GREEN}  $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# ---------- 环境检查 ----------
echo -e "${YELLOW}[1/5] 环境检查...${NC}"

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

# ---------- 配置加载 ----------
echo -e "${YELLOW}[2/5] 加载配置...${NC}"

# 读取配置 (兼容 key=value 和 key = value 格式)
load_config() {
    local key=$1
    local val=$(grep -m1 "^${key}\s*=" .env 2>/dev/null | sed 's/^[^=]*=\s*//' | tr -d '"' | tr -d "'" | xargs)
    echo "$val"
}

APP_ID=$(load_config "BOT_FEISHU_APP_ID")
APP_SECRET=$(load_config "BOT_FEISHU_APP_SECRET")
AI_MODEL=$(load_config "BOT_AI_MODEL")

echo -e "       模型:     ${GREEN}${AI_MODEL:-未配置}${NC}"
echo -e "       飞书App:  ${GREEN}${APP_ID:0:16}...${NC}"

if [ -z "$APP_ID" ] || [ "$APP_ID" = "cli_xxx" ]; then
    echo -e "${YELLOW}  ⚠ BOT_FEISHU_APP_ID 未配置, 飞书机器人将无法连接${NC}"
fi

echo -e "${GREEN}  ✓ 配置加载完成${NC}"
echo ""

# ---------- 启动调度器 ----------
echo -e "${YELLOW}[3/5] 启动新闻抓取调度器...${NC}"

mkdir -p logs

python3 run_scheduler.py > logs/scheduler.log 2>&1 &
SCHEDULER_PID=$!
echo $SCHEDULER_PID > logs/scheduler.pid

sleep 1
if kill -0 "$SCHEDULER_PID" 2>/dev/null; then
    echo -e "${GREEN}  ✓ 调度器已启动 (PID: $SCHEDULER_PID)${NC}"
    echo -e "      日志: ${GREEN}logs/scheduler.log${NC}"
    echo ""
    echo -e "${GREEN}--- 调度器启动日志 ---${NC}"
    tail -5 logs/scheduler.log 2>/dev/null | while IFS= read -r line; do
        echo -e "       $line"
    done
    echo -e "${GREEN}-----------------------${NC}"
else
    echo -e "${RED}  ✗ 调度器启动失败, 查看日志:${NC}"
    echo -e "${RED}$(tail -10 logs/scheduler.log 2>/dev/null)${NC}"
fi
echo ""

# ---------- 启动飞书机器人 ----------
echo -e "${YELLOW}[4/5] 启动飞书互动助手...${NC}"

PYTHONUNBUFFERED=1 python3 -u run_bot.py &
BOT_PID=$!
echo $BOT_PID > logs/bot.pid

sleep 4
if kill -0 "$BOT_PID" 2>/dev/null; then
    echo -e "${GREEN}  ✓ 飞书机器人已启动 (PID: $BOT_PID)${NC}"
else
    echo -e "${RED}  ✗ 飞书机器人启动失败${NC}"
fi
echo ""

# ---------- 启动 Web 服务 ----------
echo -e "${YELLOW}[5/5] 启动 Web 管理界面...${NC}"
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  所有服务已启动${NC}"
echo -e ""
echo -e "  Web 管理:  ${GREEN}http://localhost:8000${NC}"
echo -e "  API 文档:  ${GREEN}http://localhost:8000/docs${NC}"
echo -e "  调度器PID: ${GREEN}$SCHEDULER_PID${NC}"
echo -e "  机器人PID: ${GREEN}$BOT_PID${NC}"
echo -e ""
echo -e "  调度器日志: ${GREEN}tail -f logs/scheduler.log${NC}"
echo -e "  机器人日志: ${GREEN}tail -f logs/bot.log${NC}"
echo -e ""
echo -e "  ${YELLOW}Ctrl+C 停止所有服务${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload