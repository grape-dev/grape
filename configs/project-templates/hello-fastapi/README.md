# Hello FastAPI

极简 FastAPI 服务，提供 `/` 和 `/health` 两个接口。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

访问 http://localhost:8000 ，接口文档 http://localhost:8000/docs

## 学习要点

- FastAPI 声明式路由
- 返回 JSON 字典
- 自动生成交互式 API 文档（/docs）
