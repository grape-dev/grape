"""Agentic DevOps API 启动入口"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.api.main:app", host="127.0.0.1", port=3000, reload=True)
