# Webhook 端口分离设计方案

> 将 Webhook 端点与主 API/SPA 分离到独立端口 8080，主服务保持 80。
> 复用 master 分支的"一个入口、两个端口"模式。

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

## 方案：单入口 + multiprocessing

参考 master 分支设计（`src/main.py` 一个入口启动多端口服务），feat/v2 使用 `multiprocessing` 从单一 Python 入口启动两个 uvicorn 子进程：

| 组件 | 说明 |
|------|------|
| `src/api/run.py` | 生产入口点，`multiprocessing.Process` 启动 main + webhook |
| `src/api/run_webhook.py` | 轻量 FastAPI app，仅挂 `webhook.router` |
| `src/api/run.py --dev` | 本地开发模式，单端口 3000 + reload |

**进程模型：**

```
run.py (主进程, PID 1)
  ├── 子进程: uvicorn main:app → 端口 80 (SPA + API + Feishu WS)
  └── 子进程: uvicorn run_webhook:app → 端口 8080 (仅 webhook)
```

两个子进程均为 `uvicorn.Server` 实例，各自持有独立事件循环和信号处理，互不干扰。

---

## 实现细节

### 1. 新建 `src/api/run.py`（生产入口）

```python
"""Single entrypoint — one launcher, two ports: 80 (main) + 8080 (webhook)"""
import uvicorn, multiprocessing, signal, sys

def _start_main():
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=80, log_level="info")

def _start_webhook():
    uvicorn.run("src.api.run_webhook:app", host="0.0.0.0", port=8080, log_level="info")

if __name__ == "__main__":
    # --dev 模式：本地开发，单端口 + reload
    if len(sys.argv) > 1 and sys.argv[1] == "--dev":
        uvicorn.run("src.api.main:app", host="127.0.0.1", port=3000, reload=True)
        sys.exit(0)

    # 生产模式：双端口
    p1 = multiprocessing.Process(target=_start_main, name="main-api")
    p2 = multiprocessing.Process(target=_start_webhook, name="webhook")
    p1.start()
    p2.start()

    def _shutdown(*_):
        p1.terminate()
        p2.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    p1.join()
    p2.join()
```

### 2. 保留 `src/api/run_webhook.py`

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

### 3. 删除 `entrypoint.sh`

不再需要 shell 脚本——全部由 `run.py` 管理。

### 4. Dockerfile 改动

```dockerfile
# 修改前
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]

# 修改后
CMD ["python3", "-m", "src.api.run"]
```

### 5. docker-compose.yml 改动

```yaml
services:
  api:
    build: .
    ports:
      - "80:80"          # 主服务（原 3000 → 改为 80）
      - "8080:8080"      # Webhook 独立端口
    volumes:
      - ./data:/app/data
      - ./configs:/app/configs
    environment:
      - OAUTH_REDIRECT_URI=https://your-domain.com/api/auth/callback/gitcode
    restart: unless-stopped
```

### 6. `src/api/run.py`（旧、locahost 开发入口）

原 `src/api/run.py` 内容（`uvicorn.run("...main:app", host="127.0.0.1", port=3000, reload=True)`）被新 `run.py` 的 `--dev` 模式覆盖。

### 7. config.py 自动检测

**无需改动。** 当前代码自动检测到的 webhook_url 已经是 `http://{ip}:8080/webhook/gitcode`（LINE 53, LINE 60）。

### 8. settings.py 默认值

**无需改动。** 当前默认值已是 `"http://localhost:8080/webhook/gitcode"`（LINE 120）。

---

## 数据一致性

两个子进程共享 `./data/sessions.db`（SQLite）：

| 操作 | Main 子进程 | Webhook 子进程 |
|------|-------------|----------------|
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

```bash
# 模式 1: 双端口生产模式（同线上行为）
python src/api/run.py

# 模式 2: 单端口开发模式（带热重载）
python src/api/run.py --dev
```

验证 webhook 是否正常：

```bash
curl -s -X POST "http://localhost:8080/webhook/gitcode" \
  -H "Content-Type: application/json" \
  -H "X-Gitcode-Event: Push Hook" \
  -H "X-Gitcode-Token: test" \
  -d '{"ref":"refs/heads/main","project":{"path_with_namespace":"test/test"}}'
```

主服务仍可通过 `http://localhost:80` 访问 SPA。

---

## 远程服务器部署

当前远程服务器（203.0.113.2）无 Docker，为裸机 Python 部署。

```bash
# 1. 杀掉旧进程
fuser -k 80/tcp
fuser -k 8080/tcp

# 2. 启动（一个新入口启动两个端口）
cd /opt/grape && \
OAUTH_REDIRECT_URI=http://203.0.113.2/api/auth/callback/gitcode \
nohup python3 -m src.api.run > /tmp/devops-prod.log 2>&1 &
```

---

## 测试策略

| 测试 | 覆盖 |
|------|------|
| `test_webhook_push` | Webhook 处理事件正常 |
| `test_webhook_invalid_token` | Token 校验正常 |
| `test_health` | 主服务健康检查正常 |

无需新增测试文件，现有 webhook 测试保持通过即可。

---

## 回滚方案

- `git revert` 相关 commit
- 恢复旧的 `src/api/run.py`（单进程 dev 模式）
- 移除 docker-compose 中的 8080 端口映射
- 远程服务器：切回单进程 `python -m uvicorn src.api.main:app --port 80`

## 与 master 对比

| 维度 | master | feat/v2 (本方案) |
|------|--------|------------------|
| 入口 | `src/main.py` | `src/api/run.py` |
| 进程模型 | 主线程 HTTPServer:8080 + 线程 admin:80 | `multiprocessing` 两个子进程 |
| 端口 80 | admin server（历史遗留） | SPA + API |
| 端口 8080 | HTTPServer（主服务） | Webhook 端点 |
| Web 框架 | 内置 http.server | FastAPI + uvicorn |
| 部署方式 | `python3 -m src.main` | `python3 -m src.api.run` |
