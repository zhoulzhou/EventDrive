import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("正在导入 FastAPI 和 Uvicorn...")
from fastapi import FastAPI
import uvicorn
import asyncio
print("✓ 导入成功")

print("\n创建应用...")
app = FastAPI(title="测试应用", version="1.0.0")
print("✓ 应用创建成功")

print("\n添加路由...")
@app.get("/")
def read_root():
    return {"Hello": "World", "status": "running"}
print("✓ 路由添加成功")

print("\n正在启动服务器...")
print("=" * 60)
print("访问地址: http://127.0.0.1:8000")
print("=" * 60)

try:
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        loop="asyncio"
    )
except Exception as e:
    print(f"启动失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
