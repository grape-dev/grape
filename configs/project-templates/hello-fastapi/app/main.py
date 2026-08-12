"""Hello FastAPI —— 极简 FastAPI 服务"""
from fastapi import FastAPI

app = FastAPI(title="Hello FastAPI", version="1.0.0")


@app.get("/")
def root():
    return {"message": "Hello, World!"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "hello-fastapi"}
