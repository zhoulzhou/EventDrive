#!/bin/bash

# EventDrive 停止脚本 (Ubuntu 22.04)
# 停止所有后台服务: 调度器 + 飞书机器人

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

stop_pid() {
    local pid_file=$1
    local name=$2
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo -e "${GREEN}✅ 已停止 $name (PID: $pid)${NC}"
        else
            echo -e "${RED}⚠️  $name 进程不存在 (PID: $pid)${NC}"
        fi
        rm -f "$pid_file"
    else
        echo -e "${RED}⚠️  未找到 $name PID 文件${NC}"
    fi
}

echo "正在停止 EventDrive 后台服务..."

stop_pid "logs/scheduler.pid" "新闻调度器"
stop_pid "logs/bot.pid" "飞书机器人"

echo "完成"