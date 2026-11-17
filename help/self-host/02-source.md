# 3. 源码部署

> 适合要改代码、要调试、要接自定义依赖的用户。不想碰 Python 环境就走 [Docker 部署](./01-docker.md)。

## 3.1 前置条件

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **3.10+（必须）** | 低于 3.10 启动时会显式提示并退出（`src/api/version_check.py` 的 `_MIN_PYTHON`） |
| Node.js | 以 `opencode` 官方安装说明为准 | 只在用 `npm install -g opencode` 装 Agent CLI 时需要；平台本体不依赖 Node |
| Git | 任意较新版本 | |

Agent CLI 至少装一个，否则平台能启动但流水线跑不起来：

```bash
npm install -g opencode     # 基于 Node.js
# 或
pip install codearts-cli    # 基于 Python
```

## 3.2 安装依赖

**Linux / macOS**

```bash
git clone https://github.com/grape-dev/grape.git
cd grape

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local.txt     # 最小依赖（不含 OBS/MySQL/飞书）
# pip install -r requirements.txt         # 完整依赖，生产建议

cp .env.example .env
mkdir -p data
```

**Windows（PowerShell）**

```powershell
git clone https://github.com/grape-dev/grape.git
cd grape
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data
```

仓库自带一键脚本（自动建 venv、装依赖、复制 `.env`、自检、启动）：
Windows 用 `.\start.ps1`（完整依赖加 `-Full`，跳过自检加 `-SkipDoctor`）；
Linux/macOS 的开发启动入口是 `python start_server.py`（`src/api/main:app`，`3000` 端口，带 reload）。

## 3.3 配置 `.env`

和 Docker 路径一样，先把 `JWT_SECRET` 换成随机值：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# 写入 .env 的 JWT_SECRET=
```

默认 `DEVOPS_DB_BACKEND=sqlite`，不需要 MySQL。要换 MySQL 见 [数据库后端与官方同步](../developer/05-database-and-sync.md)。

## 3.4 启动

```bash
python -m src.api.run --dev      # 开发模式：单进程，端口 3000，代码热重载
```

不加 `--dev` 是生产模式：主服务 `3000` + 独立 webhook 进程 `8080`（`src/api/run.py`），
对外通常由 nginx 反代 `80/443`，不要把 `3000` / `8080` 直接暴露到公网。

## 3.5 验证

```bash
curl -f http://localhost:3000/api/health
```

返回 JSON 形如 `{"service": "<进程名>", "status": "ok", "uptime": <秒>}`
（见 `src/kernel/gsb/health.py`），`status` 为 `ok` 即服务在线。浏览器打开 `http://localhost:3000/` 能看到控制台。
登录前还需要配登录渠道（见 [安装部署总览](./00-overview.md) 的「装好之后」一节），否则登录页没有可用入口。

**成功标志**：进程不退出 + `/api/health` 返回 200 且 `status` 为 `ok` + 页面能打开。

## 3.6 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即退出并提示 Python 版本 | 低于 3.10 | 换 3.10+ 解释器重建 venv |
| `pip install` 超时 | PyPI 慢 | `pip install -r requirements-local.txt -i https://mirrors.aliyun.com/pypi/simple/` |
| 启动后 3000 连不上 | 端口被占 / 防火墙 | `ss -lntp \| grep 3000`；云主机放行安全组 |
| 流水线一直不执行 | 没装 Agent CLI，或它不在服务进程的 PATH 里 | 装 `opencode` 或 `codearts-cli`；在启动服务的同一个用户下执行 `which opencode` 确认可执行 |
| OAuth 回调失败 | `server_base_url` 没配，回调落在 localhost | `cp configs/server-config.example.json configs/server-config.json`（该真源不入库），再在文件或管理面把 `server_base_url` 配成浏览器可达的地址 |

## 3.7 更新到新版本

```bash
git pull
source .venv/bin/activate
pip install -r requirements-local.txt   # 依赖有变动时
```

单机 SQLite 模式：重启进程即可，迁移在启动时执行。

分域库 / MySQL 多库模式：**不能直接重启**，按「迁移 → 视图修复 → 复核 → 重启」的顺序先跑
`bash scripts/deploy-after-migrate.sh`（非 0 不得重启），细节见 `deploy/README.md`。

用 systemd 托管时，仓库 `deploy/systemd/` 提供四个应用单元（`grape-server`、`grape-event-server`、
`grape-worker-server`、`grape-insight-server`）以及巡检用的 drain/reconcile 单元与 timer，
安装脚本是 `scripts/install-grape-systemd.sh`；重启按实际启用的单元名执行。

升级前先备份，见 [升级、备份与回滚](./03-upgrade-backup.md)。