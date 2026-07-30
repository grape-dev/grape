# Webhook 端口分离设计方案

> 将 Webhook 端点与主 API/SPA 分离到独立端口 8080，主服务保持 80/3000。

---

## 背景

当前 Webhook 端点 `POST /webhook/gitcode` 与用户管理网页（SPA + API）共用同一端口（80）。为满足精细化运维和安全隔离需求，将 Webhook 独立到 **8080 端口**。

**架构总览：**

```
Internet
  │
  ├── http://example.com (Port 80)     → SPA + 用户 API + Feishu WS
  └── http://example.com:8080          → Webhook 端点 (仅)
```

---

## 方案：单容器双进程

同一 Docker 容器内启动两个 uvicorn 进程，共享代码和数据卷。

| 端口 | 进程 | 用途 | 包含 |
|------|------|------|------|
| 80 → 3000 | `uvicorn src.api.main:app` | 主服务 | SPA + 全部 API + Feishu WS + CORS |
| 8080 | `uvicorn src.api.run_webhook:app` | Webhook | 仅 `webhook.router` + DB 初始化 |

### 优点

- 无需额外容器，部署复杂度低
- 共享同一份代码、同一份数据卷
- 两个进程互不干扰，一个挂掉不影响另一个
- 可独立扩缩容（如果未来需要）

### 风险

- SQLite 并发写锁（由 `db_lock` 协调，WAL 模式下风险可控）
- 内存占用增加（多一个 Python 进程约 30-50MB）

---

## 实现细节

### 1. 新建 `src/api/run_webhook.py`

轻量级 FastAPI 应用，仅包含 Webhook 路由和必要基础设施：

```python
"""Webhook-only server — port 8080"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from src.api.routes.webhook import router as webhook_router
from src.api.db_setup import init_database
from src.config import _load_accounts, log

app = FastAPI(title="Agentic DevOps Webhook", version="2.0.0")
app.include_router(webhook_router)

# DB 初始化（与主服务共用 data/sessions.db）
init_database()
_load_accounts()

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    log.warning(f"Webhook error: {exc}")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"code": 500, "message": str(exc)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.run_webhook:app", host="0.0.0.0", port=8080)
```

**关键点：**
- 不挂 `auth`、`repos`、`chat` 等用户路由，减少攻击面
- 不启动 Feishu WS（主进程已负责）
- 不挂 SPA 静态文件
- DB 初始化后 `_load_accounts()` 确保 get_webhook_token_by_repo 可用

### 2. 新建 `entrypoint.sh`

启动脚本，同时拉起两个进程：

```bash
#!/bin/bash
set -e

# 启动主服务（SPA + API）
uvicorn src.api.main:app --host 0.0.0.0 --port 3000 &

# 启动 Webhook 服务
uvicorn src.api.run_webhook:app --host 0.0.0.0 --port 8080 &

# 等待所有后台进程
wait -n
```

### 3. Dockerfile 改动

将 CMD 从直接 uvicorn 改为 entrypoint：

```dockerfile
# 修改前
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]

# 修改后
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
```

### 4. docker-compose.yml 改动

增加 8080 端口映射：

```yaml
services:
  api:
    build: .
    ports:
      - "80:3000"
      - "8080:8080"     # 新增：Webhook 独立端口
    volumes:
      - ./data:/app/data
      - ./configs:/app/configs
    environment:
      - OAUTH_REDIRECT_URI=https://your-domain.com/api/auth/callback/gitcode
    restart: unless-stopped
```

### 5. config.py 自动检测

**无需改动。** 当前代码自动检测到的 webhook_url 已经是 `http://{ip}:8080/webhook/gitcode`（LINE 53, LINE 60），与本方案一致。

### 6. settings.py 默认值

**无需改动。** 当前默认值已是 `"http://localhost:8080/webhook/gitcode"`（LINE 120）。

---

## 数据一致性

两个进程共享 `./data/sessions.db`（SQLite）：

| 操作 | 主进程 | Webhook 进程 |
|------|--------|-------------|
| `managed_repos` 读 | ✅ | ✅ |
| `managed_repos` 写 | ✅ | ❌（只读，不做 join/host） |
| `event_log` 读 | ✅ | ❌ |
| `event_log` 写 | ❌ | ✅（webhook 写入事件） |
| `chat_messages` 读写 | ✅ | ❌ |

SQLite WAL 模式下读不阻塞写，写不阻塞读。`db_lock` 以进程内锁保护，跨进程由 SQLite 自身行锁/重试机制协调。

**潜在风险：** 高并发 webhook + 高并发 API 写入可能导致 `database is locked` 错误。应对方案：
- 低并发场景（<50 req/s）无影响
- 高并发时可切换到 PostgreSQL（适配器模式已预留）

---

## 本地开发

在本地同时启动两个进程：

```bash
# 终端 1: 主服务（带热重载）
python src/api/run.py

# 终端 2: Webhook 服务（带热重载）
uvicorn src.api.run_webhook:app --reload --host 0.0.0.0 --port 8080
```

验证 webhook 是否正常：

```bash
curl -s -X POST "http://localhost:8080/webhook/gitcode" \
  -H "Content-Type: application/json" \
  -H "X-Gitcode-Event: Push Hook" \
  -H "X-Gitcode-Token: test" \
  -d '{"ref":"refs/heads/main","project":{"path_with_namespace":"test/test"}}'
```

主服务仍可通过 `http://localhost:3000` 访问 SPA。

---

## 测试策略

| 测试 | 覆盖 |
|------|------|
| `test_webhook_push` | Webhook 进程处理事件正常 |
| `test_webhook_invalid_token` | Token 校验工作在 8080 端口 |
| `test_health` (port 80) | 主服务健康检查正常 |
| E2E | 启动双进程后验证两个端口均正常工作 |

无需新增测试文件，只需确保现有 webhook 测试在单进程模式下通过即可。

---

## 回滚方案

- 恢复 Dockerfile 的 CMD 为原始单进程模式
- 移除 docker-compose 中的 8080 端口映射
- 删除 `src/api/run_webhook.py` 和 `entrypoint.sh`

