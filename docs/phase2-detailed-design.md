# 阶段 2 详细改动方案——容器化部署（Docker）

## 背景

阶段 1 已完成（健康监控 + 取消实时性），代码在 `feat/health-monitoring` 分支。
阶段 2 将 grape backend 容器化——Dockerfile + docker-compose + 迁移锁。

**nginx 不容器化——保留宿主机。** nginx 是基础设施不是 grape 服务，不应该耦合进服务部署。

## 当前部署方式

- 启动方式：`python src/api/run.py`（multiprocessing 启动两个 uvicorn：3000 主服务 + 8080 webhook）
- nginx 反代：宿主机 nginx，配置在 /etc/nginx/conf.d/grape-admin.conf（需 sudo 读取）
- 依赖管理：requirements.txt 已有（paramiko/lark-oapi/fastapi/uvicorn/aiohttp/pymysql/esdk-obs-python 等）
- Worker：独立进程，不容器化（需要 hcloud CLI / git / opencode 等宿主机工具）

## 改动点

### 改动点 1：新增 Dockerfile

**理由：** 容器化部署需要 Dockerfile 打包 backend。

**方案：**

```dockerfile
FROM python:3.14-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码
COPY . .

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

EXPOSE 3000

# 只启动 3000 主服务——main:app 已包含所有路由（含 webhook + A2A + runtime API）
# 不启动 8080——run_webhook:app 是冗余的，main:app 已注册 webhook_router
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

- 只启动 3000——main:app 已注册所有路由（Web 页面 + API + runtime + webhook + A2A）
- 不启动 8080——run_webhook:app 是冗余的，main:app 已有 webhook_router（main.py 第 119 行）
- 不需要 supervisord——单进程 uvicorn
- python:3.14-slim 基础镜像
- 装 gcc/g++ 编译 C 扩展（paramiko/pymysql 等）
- TZ=Asia/Shanghai 时区配置

**hcloud CLI 不装：** backend 容器不执行 hcloud 命令——hcloud 由 worker 在宿主机上执行。backend 只做 API 服务 + 任务调度。

### 改动点 2：新增 .dockerignore

**理由：** COPY . . 会把 .git/.venv/data/logs 等打进镜像——体积膨胀。

**方案：**

```
.git
.venv
data
logs
__pycache__
*.pyc
*.pyo
.gitignore
.env
*.md
docs/
skills/tools/
skills/huawei-cloud-ecs-servers/
branches-backup-*.bundle
configs/
```

**configs/ 整个目录排除——通过 docker-compose volume 挂载：**
- docker-compose.yml volumes: `./configs:/app/configs`
- 运行时从宿主机读取——修改配置不用重新 build 镜像
- 敏感文件（admin.env / db-backend.json）不打进镜像
- 非敏感文件（skill-description.json 等）也不打进镜像——volume 挂载提供
- configs/*.example.json 被一起排除了——但 .example 文件在 git 里有，docker build 时不在了

**注意：** 由于 configs/ 整个目录被排除，镜像里没有 configs/ 目录。运行时完全依赖 docker-compose volume 挂载。如果不用 docker-compose 启动（直接 docker run），需要手动挂载 configs/ 目录。

### 改动点 3：新增 docker-compose.yml

**理由：** 容器化 backend。nginx 保留宿主机——不容器化。

**方案：**

```yaml
services:
  backend:
    build: .
    container_name: grape-backend
    ports:
      - "127.0.0.1:3000:3000"   # 宿主机 nginx 反代到容器 3000
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - grape-data:/app/data
      - ./configs:/app/configs   # 挂载整个 configs 目录（运行时可改配置）
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  grape-data:
```

**架构设计：**

```
当前（容器化后）：
  用户 → nginx:80（宿主机）→ 127.0.0.1:3000（容器）→ uvicorn main:app

后续去掉 nginx：
  用户 → ELB 443 → 宿主机 3000（容器）→ uvicorn main:app
```

| 端口 | 绑定 | 服务 | 对外 | 说明 |
|------|------|------|:---:|------|
| 80 | 0.0.0.0 | 宿主机 nginx | ✅ | TLS 终止 + 反代到 3000 |
| 443 | 0.0.0.0 | 宿主机 nginx | ✅ | HTTPS（证书在宿主机 nginx） |
| 3000 | 127.0.0.1 | 容器 backend | ❌ | 宿主机 nginx 反代到容器 |

**为什么 nginx 不容器化：**
- nginx 是基础设施不是 grape 服务——不应该耦合进服务部署
- 宿主机已有 nginx 在跑——再起 nginx 容器 80 端口冲突
- 后续可以去掉 nginx——ELB 直接转发到 backend
- nginx 的作用（TLS 终止/反代/静态文件）ELB 和 FastAPI 都能做

**为什么 3000 绑定 127.0.0.1：**
- 宿主机 nginx 通过 127.0.0.1:3000 反代到容器
- 3000 不暴露公网——外部访问走 nginx 80/443
- 和当前架构一致——nginx 代理到 127.0.0.1:3000

**Worker 连接方式：**

| Worker 位置 | RUNTIME_SERVER_URL | 路径 |
|------------|-------------------|------|
| 本机 | http://localhost:3000 | 宿主机 3000 → 容器 3000 |
| 其他虚机 | http://域名或IP | ELB → nginx:80 → 容器:3000 |

- Worker 所有 API（/api/runtime/*）都在 main:app（3000）上
- 本机 Worker 不需要改——还是连 localhost:3000
- 其他虚机 Worker 连域名/IP → ELB → nginx → 3000

**关键设计：**
- 3000 绑定 127.0.0.1——宿主机 nginx 反代到容器
- 8080 不启动——main:app 已有 webhook 路由，CMD 只启动 uvicorn 3000
- env_file: .env——敏感信息注入（DB/OBS 配置）
- healthcheck 用阶段 1 的 /api/health
- restart: unless-stopped
- data/logs 持久化 volume

**日志输出（双渠道）：**
- config.py 第 5-11 行：logging 同时配了 TimedRotatingFileHandler（data/devops-bot.log）+ StreamHandler（stdout）
- docker logs grape-backend——看 stdout 日志（uvicorn + application log）
- data/devops-bot.log——volume 持久化日志文件（含轮转文件 .2026-08-14 等）
- 两条渠道都可用——docker logs 实时看 + volume 持久化历史

### 改动点 4：新增 .env.example

**理由：** 敏感信息不写进 Dockerfile/docker-compose，用 .env 文件注入。

**方案：**

```bash
# .env.example——复制为 .env 后填写实际值
# .env 不提交到 git（已在 .gitignore）

# 数据库（mysql_compat.py 支持 DEVOPS_DB_* 环境变量覆盖 configs/db-backend.json）
DEVOPS_DB_BACKEND=mysql
DEVOPS_DB_HOST=127.0.0.1
DEVOPS_DB_PORT=3306
DEVOPS_DB_USER=grape
DEVOPS_DB_PASSWORD=your-db-password
DEVOPS_DB_NAME=rds-grape-dev

# OBS
OBS_ENABLED=true
OBS_BUCKET=obs-grape-dev-file
OBS_ACCESS_KEY=your-access-key
OBS_SECRET_KEY=your-secret-key
OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com
OBS_PREFIX=grape/prod
```

**注意：**
- DB 配置通过 DEVOPS_DB_* 环境变量注入——不依赖 configs/db-backend.json（含明文密码）
- mysql_compat.py 第 70-74 行支持环境变量覆盖：DEVOPS_DB_BACKEND / DEVOPS_DB_HOST / DEVOPS_DB_PORT / DEVOPS_DB_USER / DEVOPS_DB_PASSWORD / DEVOPS_DB_NAME
- server_base_url / webhook_url 不在 .env 里——config.py 不支持环境变量读取，通过 configs/server-config.json 配置（见改动点 3 webhook_url 配置说明）
- .env 文件含敏感信息，必须加入 .gitignore（已有）

**webhook_url 配置（grape 原有逻辑，不需要改代码）：**

config.py 第 104-125 行 `_load_accounts()` 中 webhook_url/server_base_url 的获取优先级（代码原有）：

1. 数据库 meta 表（`get_meta_value("webhook_url")`）——已有值就直接用，不再检测
2. configs/server-config.json（`read_server_config()`，config.py 第 170 行）——文件不存在返回 `{}`
3. `_detect_server_base()` 自动检测（config.py 第 79-98 行）——调 api.ipify.org 拿公网 IP，拼 `http://{IP}:8080`

**当前系统实际情况：** 第一次部署时数据库空 + 没有 server-config.json → 走第 3 级自动检测 → 拿到 `http://1.92.115.229:8080` → 写入数据库 meta 表。之后每次启动走第 1 级直接用数据库的值。

**容器化后的影响：** 全新环境（空数据库）+ 没有 server-config.json → 走第 3 级自动检测 → 返回 `http://{IP}:8080`——但 8080 没启动（CMD 只启动 3000），地址错误。

**解决方案：** configs/ 整个目录通过 docker-compose volume 挂载。全新环境部署前在宿主机 configs/server-config.json 配置正确地址（走第 2 级，跳过自动检测）：

```json
{
  "server_base_url": "https://your-server-domain",
  "webhook_url": "https://your-server-domain/webhook/gitcode"
}
```

| 场景 | 需要 server-config.json？ | 原因 |
|------|:---:|------|
| 已有数据库的环境 | ❌ | meta 表已有值，走第 1 级 |
| 全新环境 + 有 server-config.json | ✅ | 走第 2 级，跳过自动检测 |
| 全新环境 + 无 server-config.json | ❌ 会出错 | 走第 3 级，返回 http://{IP}:8080（8080 没启动） |

### 改动点 5：数据库迁移锁（src/api/db_setup.py）

**理由：** 阶段 3 多实例同时启动时会同时跑迁移导致冲突。阶段 2 提前准备好。

**方案：**

在 `init_database()` 函数开头加迁移锁：

```python
def init_database():
    """初始化所有新表（幂等），启动时调用"""
    # 迁移锁——MySQL 用 GET_LOCK 防多实例同时迁移
    # SQLite 不支持 GET_LOCK，跳过（单实例不需要）
    from src.mysql_compat import get_db_backend
    if get_db_backend() == "mysql":
        with db_lock:
            c = db.cursor()
            c.execute("SELECT GET_LOCK('grape_migrate', 300)")
            lock_acquired = c.fetchone()[0]
            if not lock_acquired:
                log.warning("Could not acquire migrate lock, waiting...")
            try:
                # 迁移逻辑在同一个 with db_lock 块内——同一个连接
                # db_lock 是 RLock（可重入）——_create_tables() 等内部 with db_lock 不会死锁
                _create_tables()
                _migrate_tables()
                _create_indexes()
                _seed_from_json()
                _sync_official_skills()
                _align_agent_enable()
                _cleanup_stale_meta()
                _cleanup_chat_shared_worker_runtime()
                try:
                    from src.runtime.engine_select import backfill_existing_bindings
                    backfill_existing_bindings()
                except Exception as e:
                    log.warning(f"Backfill existing runtime bindings failed: {e}")
            finally:
                c.execute("SELECT RELEASE_LOCK('grape_migrate')")
    else:
        # SQLite——单实例不需要锁
        _create_tables()
        _migrate_tables()
        _create_indexes()
        _seed_from_json()
        _sync_official_skills()
        _align_agent_enable()
        _cleanup_stale_meta()
        _cleanup_chat_shared_worker_runtime()
        try:
            from src.runtime.engine_select import backfill_existing_bindings
            backfill_existing_bindings()
        except Exception as e:
            log.warning(f"Backfill existing runtime bindings failed: {e}")
```

**关键设计：**
- 用 `from src.mysql_compat import get_db_backend` 判断后端类型（不是 `from src.config import DB_BACKEND`——config.py 里没有这个变量）
- get_db_backend() 支持环境变量 DEVOPS_DB_BACKEND 优先，其次 configs/db-backend.json，默认 sqlite（mysql_compat.py 第 42-52 行）
- GET_LOCK('grape_migrate', 300)——等待最多 300 秒
- GET_LOCK + 迁移 + RELEASE_LOCK 在同一个 with db_lock 块内——同一个 MySQL 连接，避免连接断开重连导致锁提前释放
- db_lock 是 threading.RLock()（可重入锁，session_manager.py 第 39 行）——_create_tables() / _migrate_tables() 等函数内部的 with db_lock 不会死锁
- finally 里 RELEASE_LOCK——确保锁释放
- 迁移逻辑不变——只是包在锁里

**为什么放在同一个 with db_lock 块：**

MySQL GET_LOCK 是连接级锁——连接断开时锁自动释放。如果 GET_LOCK 和迁移不在同一个 with db_lock 块：
- GET_LOCK 拿到锁后释放 db_lock
- 迁移过程中 MySQL 连接可能断开重连（conn.ping(reconnect=True)）
- 重连后新连接上没有锁——锁已提前释放
- 其他实例能拿到锁——两个实例同时迁移冲突

放在同一个 with db_lock 块——同一个连接从头到尾，减少连接断开风险。

## 改动汇总

**新增文件（4 个）：**

| # | 文件 | 内容 |
|---|------|------|
| 1 | Dockerfile | Python 3.14 + run.py 双进程启动 |
| 2 | .dockerignore | 排除 .git/.venv/data/logs 等 |
| 3 | docker-compose.yml | 只容器化 backend，healthcheck + restart + volumes |
| 4 | .env.example | 敏感信息模板 |

**修改文件（1 个）：**

| # | 文件 | 改动 |
|---|------|------|
| 5 | src/api/db_setup.py | init_database() 加迁移锁（MySQL GET_LOCK，SQLite 跳过） |

**不改动：**
- requirements.txt（已有）
- src/api/run.py（已有双进程启动）
- Worker（不容器化，连 localhost:3000 不变）
- 宿主机 nginx（保留，不容器化）

**零新依赖。** nginx 保留宿主机——不耦合进服务部署。

## 验证步骤

1. 创建 .env：`cp .env.example .env`，填入 DB 密码和 OBS AK/SK
2. 全新环境创建 configs/server-config.json：`cp configs/server-config.example.json configs/server-config.json`，填入域名地址（已有数据库的环境跳过）
3. `docker compose build`——构建镜像
4. `docker compose up -d`——启动 backend 容器
5. `curl http://127.0.0.1:3000/api/health`——验证健康端点
6. `curl http://localhost:80/api/health`——验证 nginx 反代
7. Worker 连 `http://localhost:3000` 验证注册
8. 前端访问 `http://localhost` 验证页面加载

## 注意事项

1. .env 文件不提交到 git——含 AK/SK/DB 密码等敏感信息
2. data/ 目录挂载到 volume——OBS 模式下 data/devops-bot.log + cache/ 仍需持久化
3. Worker 不进 Docker——需要 hcloud CLI / git / opencode 等宿主机工具
4. 迁移锁只在 MySQL 下生效——SQLite 单实例不需要
5. nginx 保留宿主机——不容器化，不耦合进服务部署
6. backend 3000 绑定 127.0.0.1——宿主机 nginx 反代到容器
7. 本机 Worker 不需要改——还是连 localhost:3000
8. 后续可去掉 nginx——ELB 直接转发到 backend 3000
9. 8080 不启动——main:app（3000）已有 webhook 路由，CMD 只启动 uvicorn 3000
10. HTTPS 由宿主机 nginx 或 ELB 终止——backend 只处理 HTTP
11. DB 密码用 DEVOPS_DB_* 环境变量注入——不依赖 configs/db-backend.json
12. configs/ 整个目录通过 volume 挂载——不打进镜像，运行时可改配置
13. 迁移锁用 `from src.mysql_compat import get_db_backend` 判断后端（不是 config.py 的 DB_BACKEND）
14. 日志双渠道输出——docker logs（stdout，max 50m x 3）+ data/devops-bot.log（grape-data volume 持久化）
15. init_database() 在 main.py 第 142-143 行调用——uvicorn 加载 main.py 时自动执行，不需要额外调用
16. 全新环境部署前需创建 configs/server-config.json——配置 webhook_url 和 server_base_url（已有数据库的环境不需要）
17. uvicorn 默认单 worker——main.py 有全局变量 bot_manager + init_database()，多 worker 会重复初始化
18. GET_LOCK + 迁移 + RELEASE_LOCK 在同一个 with db_lock 块——同一个连接，避免锁提前释放
