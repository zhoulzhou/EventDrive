import uvicorn

if __name__ == "__main__":
    print("启动新闻抓取应用服务器...")
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info"
    )
