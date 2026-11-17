# 2. Docker 部署（推荐）

> 适合想尽快跑起来、不碰 Python 环境的用户。全程只依赖 Docker，不需要在宿主机装 Python 或 Node。
> 要改代码、要调试，走 [源码部署](./02-source.md)。装完之后的配置见 [平台配置总览](../developer/00-overview.md)。

> **先看清楚这套 compose 的拓扑**：`deploy/docker-compose.yml` 是**四容器 + nginx** 布局，不是单容器。
> 四个应用容器各跑一个 uvicorn 进程，分别监听 `38080`-`38083`；只有 `grape-nginx` 对外发布端口（`80:80`），
> 由 `deploy/nginx-grape-compose.conf` 按路径分流。下文所有命令都以这个布局为准。

## 2.1 前置条件

| 项 | 要求 | 怎么确认 |
|---|---|---|
| Docker Engine | 能运行 `docker compose` 的版本（仓库未声明最低版本，以 Docker 官方支持矩阵为准） | `docker version` |
| Docker Compose | `docker compose` 子命令；compose 文件用了 `env_file` 的 `path`/`required` 长语法，旧版解析会失败 | `docker compose version` |
| 网络 | 构建时要能访问 pip 源（镜像在本地构建，不拉官方镜像） | `pip` 源可达即可 |
| 端口 | 宿主 `80` 空闲（nginx 对外唯一端口；`38080`-`38083` 只在 compose 网络内） | `ss -lntp \| grep :80` |

首次构建会在本地编译镜像（含 bcrypt / cffi 等 C 扩展），`deploy/Dockerfile` 里已经装了 `gcc` / `g++`，宿主机不用装。
构建耗时取决于网络与机型，仓库不提供固定估值。

## 2.2 准备 `.env`（放仓库根）

```bash
git clone https://github.com/grape-dev/grape.git
cd grape
cp .env.example .env
```

`.env` **必须放在仓库根**，原因有两层，缺一层都会启动失败：

1. `deploy/docker-compose.yml` 的 `env_file` 指向 `../.env`（`required: false`），容器内的数据库/业务配置从这里来；
2. 各服务的 `JWT_SECRET=${JWT_SECRET:?JWT_SECRET required}` 是 **compose 插值**，读的是 compose 进程的环境，
   不会自动读 `env_file`。compose 默认只加载 **compose 文件所在目录**（`deploy/`）的 `.env`，所以要在命令里显式传
   `--env-file ../.env`（下文命令已带）。

`JWT_SECRET` 是**硬性必填**：为空时 `docker compose` 直接报错退出。先生成随机值再写进 `.env`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# 把输出填到 .env 的 JWT_SECRET= 这一行；不要用 .env.example 里的占位值
```

其余项默认即可：`DEVOPS_DB_BACKEND=sqlite`（不需要 MySQL）、`OBS_ENABLED=false`。
如果对外用 HTTPS，把 `COOKIE_SECURE=true`。

## 2.3 启动

```bash
cd deploy
docker compose --env-file ../.env up -d --build
docker compose --env-file ../.env ps      # 等四个应用容器 + nginx 都变成 healthy（start_period 30s）
```

首次会本地构建镜像。查看日志要指定服务名（清单见 2.3 表）：

```bash
docker compose --env-file ../.env logs -f grape-server
docker compose --env-file ../.env logs -f grape-worker-server
```

四个服务与端口对应关系：

| 服务 | 进程 | 容器内端口 |
|---|---|---|
| `grape-server` | `src.api.grape_server`（用户面 API / IM WS / 前端托管） | `38080` |
| `grape-event-server` | `src.api.grape_event_server`（webhook / A2A） | `38081` |
| `grape-worker-server` | `src.api.grape_worker_server`（runtime 协议 / tasks / chat 执行） | `38082` |
| `grape-insight-server` | `src.api.grape_insight_server`（可观测性 / 仪表盘） | `38083` |
| `grape-nginx` | nginx 按路径分流 | `80` |

## 2.4 验证

```bash
curl -f http://localhost/api/health
```

返回 JSON 形如 `{"service": "<进程名>", "status": "ok", "uptime": <秒>}`（见 `src/kernel/gsb/health.py`），
`status` 为 `ok` 即该进程在线。nginx 会按路径把请求转到对应进程，逐项确认可以分别执行：

```bash
docker compose --env-file ../.env ps          # 全部 healthy
curl -fs http://localhost/api/health          # 200 + status=ok
```

再用浏览器打开 `http://<服务器地址>/` 能看到控制台。登录前还需要配登录渠道（见 [安装部署总览](./00-overview.md) 的「装好之后」一节），否则登录页没有可用入口。

**成功标志**：`docker compose ps` 全部 `healthy` + `/api/health` 返回 200 且 `status` 为 `ok`。两条都满足再往下走。

## 2.5 数据放在哪

| 位置 | 内容 | 说明 |
|---|---|---|
| 卷 `grape-data` → `/app/data` | SQLite 数据库、任务数据 | 删容器不丢；备份方法见 [升级、备份与回滚](./03-upgrade-backup.md) |
| `../configs` → `/app/configs` | 登录渠道、场景同步、邮件、安全等配置 | bind mount 在宿主仓库的 `configs/`，改完重启对应容器生效 |
| `../.grape/skills` → `/app/.grape/skills` | 技能资产 | 同上；`skills/` 目录被 `.gitignore` 忽略，不要拿它当挂载源 |
| `../examples` → `/app/examples` | 示例（只挂到 `grape-server`） | 同上，可自己改 |

## 2.6 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `JWT_SECRET required` 后直接退出 | 没传 `--env-file`，或 `.env` 里 `JWT_SECRET` 是空值 | 在仓库根准备 `.env` 并填值；命令带 `--env-file ../.env` |
| 构建卡在 `pip install` 或超时 | 访问 PyPI 慢 / 被墙 | 换网络或配代理；也可在 `deploy/Dockerfile` 的 `pip install` 后加 `-i https://mirrors.aliyun.com/pypi/simple/` 再构建 |
| 启动报端口占用 | 宿主 `80` 已被 nginx 等占用 | 改 `deploy/docker-compose.yml` 里 `nginx` 服务的 `ports` 为 `"8080:80"`，改用 8080 访问 |
| 容器一直 `unhealthy` | 应用启动异常（依赖缺失、目录权限） | `docker compose --env-file ../.env logs <服务名>` 看首个 traceback（服务名见 2.3 表） |
| 页面能开但流水线不动 | 没有 Agent CLI，或它不在服务进程的 PATH 里 | 装 `opencode` / `codearts-cli`；在宿主机执行 `which opencode` 确认可执行，容器内则 `docker compose exec grape-worker-server which opencode` |

## 2.7 更新到新版本

```bash
git pull
cd deploy
docker compose --env-file ../.env up -d --build      # 重新构建并替换容器
```

单机 SQLite 模式：迁移在进程启动时执行（`init_database()`），重新 `up -d` 即完成。

分域库 / MySQL 多库模式：**不能直接重启**，必须按「迁移 → 视图修复 → 复核 → 重启」的顺序，先用
`scripts/deploy-after-migrate.sh` 完成前三步，脚本非 0 时不得重启。细节见 `deploy/README.md`。

升级前先备份 `grape-data` 卷与 `configs/`，见 [升级、备份与回滚](./03-upgrade-backup.md)。