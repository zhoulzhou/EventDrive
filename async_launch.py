import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 70)
print("异步启动器 - 简化版")
print("=" * 70)

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True
)

logger = logging.getLogger(__name__)

async def main():
    print("\n正在导入应用...")
    from app.main import app
    print("✓ 应用导入成功")
    print(f"应用: {app.title} v{app.version}")
    
    import uvicorn
    
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=True
    )
    
    server = uvicorn.Server(config)
    
    print("\n" + "=" * 70)
    print("🚀 正在启动服务器...")
    print("=" * 70)
    print("\n📱 访问地址: http://127.0.0.1:8000")
    print("📚 API 文档: http://127.0.0.1:8000/docs")
    print("=" * 70 + "\n")
    sys.stdout.flush()
    
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 收到停止信号，正在关闭...")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
