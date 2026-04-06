#!/bin/bash

# 新闻抓取应用启动脚本 (Ubuntu 22.04)
# 使用方法: ./run.sh

# 设置项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  新闻抓取应用启动${NC}"
echo -e "${YELLOW}============================================${NC}"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到 Python3，请先安装 Python 3.8+${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo -e "${GREEN}✅ ${PYTHON_VERSION}${NC}"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}⚠️  未找到虚拟环境，正在创建...${NC}"
    python3 -m venv venv
    echo -e "${GREEN}✅ 虚拟环境创建完成${NC}"
fi

# 激活虚拟环境
echo -e "${YELLOW}📦 激活虚拟环境...${NC}"
source venv/bin/activate

# 检查依赖
echo -e "${YELLOW}📦 检查依赖...${NC}"
pip install -q -r requirements.txt
echo -e "${GREEN}✅ 依赖已安装${NC}"

# 创建必要目录
echo -e "${YELLOW}📁 创建数据目录...${NC}"
mkdir -p data/images logs
echo -e "${GREEN}✅ 目录已创建${NC}"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  .env 文件不存在，从 .env.example 复制...${NC}"
    cp .env.example .env
    echo -e "${GREEN}✅ .env 文件已创建，请配置相关参数${NC}"
fi

echo ""
echo -e "${YELLOW}============================================${NC}"
echo -e "${GREEN}🚀 启动 FastAPI 服务器...${NC}"
echo -e "${YELLOW}============================================${NC}"
echo ""
echo -e "访问地址: ${GREEN}http://localhost:8000${NC}"
echo -e "API 文档: ${GREEN}http://localhost:8000/docs${NC}"
echo ""
echo -e "${YELLOW}按 Ctrl+C 停止服务器${NC}"
echo ""

# 启动服务器
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
