# 人工安装指南

本文件讲**手工逐步安装**：Windows / macOS / Linux / Docker 每种环境的具体步骤，与安装脚本做的事一一对应。

**安装并启动，一个命令：**

**GitHub**

```bash
git clone https://github.com/grape-dev/grape.git && python grape/quickstart.py
```

**GitCode**

```bash
git clone https://gitcode.com/grape-dev/grape.git && python grape/quickstart.py
```

> 下文手工步骤里的仓库地址以 GitHub 为准；换 GitCode 镜像只需把 `https://github.com/grape-dev/grape.git` 换成 `https://gitcode.com/grape-dev/grape.git`。

## 环境要求

| 组件 | 版本要求 | 说明 |
| --- | --- | --- |
| **Python** | >= 3.10（必须） | 主服务与 devbox sandbox 均要求 Python 3.10+（新式类型注解/语法依赖；低于 3.10 启动会显式提示并退出） |
| **Node.js** | >= 18（Agent CLI） | `npm install -g opencode` |
| **Git** | 任意较新版本 | |

## Windows（PowerShell）

```powershell
git clone https://github.com/grape-dev/grape.git
cd grape

# 一键启动：自动建 venv + 装依赖 + 复制 .env + 自检 + 启动
.\start.ps1                    # 本地开发（最小依赖、SQLite）
.\start.ps1 -Full              # 完整依赖（含 OBS/MySQL/飞书）
.\start.ps1 -SkipDoctor        # 跳过自检，直接启动

# 或手动操作：
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data
python -m src.api.run --dev    # 开发模式，打开 http://localhost:3000
```

**Windows 注意事项**

- 脚本被执行策略拦截时先执行：`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

**clone 报 `not found`，URL 里出现 `gh-proxy` 之类代理域名**

机器上装过「GitHub 加速」类工具时，git 的 `insteadOf` 规则会把 `https://github.com/` 改写到失效地址（`github.ab.com` 这类错域名会让所有 clone 一起失败）。先自检规则及其来源：

```powershell
git config --list --show-origin | Select-String insteadOf
```

清掉失效规则，然后重跑上面的命令：

```powershell
git config --global --unset-all "url.https://gh-proxy.com/https://github.ab.com/.insteadOf"
```

规则来自 `--system` 时改用 `--system`（需管理员 PowerShell）；只写在某个仓库的 `.git/config` 时，进该仓库执行不带作用域的 `git config --unset-all`。

## macOS / Linux

**或手动执行**（与脚本步骤一一对应）：

```bash
git clone https://github.com/grape-dev/grape.git && cd grape

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt                      # 完整依赖（含 OBS/MySQL/飞书）

cp .env.example .env && mkdir -p data
python -m src.api.run --dev                          # http://localhost:3000
```

**macOS / Linux 注意事项**

网络受限时走镜像：

```bash
# PyPI 阿里云镜像
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
# 永久配置：
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

`git fetch` 偶发 HTTP/2 帧错误或连接超时属网络抖动，拉取路径已做最多 3 次退避重试；仍失败可降级 HTTP/1.1 或走镜像：

```bash
git config --global http.version HTTP/1.1
git config --global url."https://<你的镜像>/https://github.com/".insteadOf "https://github.com/"
```

本机没有 Python ≥ 3.10 时，脚本会自动切换到 PATH 里可用的新版解释器（`python3.11` / `python3.12` 等）；仍然找不到时会打印用 uv 装解释器的命令 —— 无需 root，也不动系统 Python。

## Docker（可选）

用 `python quickstart.py --docker` 可让脚本自动生成 `JWT_SECRET` 并起容器。

```bash
export JWT_SECRET=$(openssl rand -hex 32)
# docker-compose.yml 声明了 ${JWT_SECRET:?JWT_SECRET required}，缺失时容器会立刻退出
docker compose up -d
```

**Docker 注意事项**

- `JWT_SECRET` 缺失时容器立刻退出；本地试用也要先 export
- WebUI 在 `http://<主机>/`（compose 映射 80:3000 与 8080:3000）；自检 `curl -fsS http://127.0.0.1:3000/api/health`

## 首次登录与零配置体验

本地启动**不需要任何 `configs/` 配置**，克隆下来直接启动即可，在 WebUI 用**用户名 + 密码注册登录**：

- 未配置邮箱服务时，注册自动跳过邮箱验证码（邮箱栏填占位地址即可，不做格式与真实性校验），注册成功即已登录
- 未配置 GitCode / GitHub OAuth 时，登录页提示"请使用用户名密码注册登录"，第三方登录按钮置灰，本地账号照常可用
- 项目模板无需手工准备：`examples/` 克隆下来只有一份 README，**首次启动自动从 [grape-template](https://github.com/grape-dev/grape-template) 拉取模板库**（启动日志 `Templates: ... verified OK` 即同步完成）

## 其余配置与上线部署

| 事项 | 位置 |
| --- | --- |
| 执行引擎与模型配置（运行前提） | [docs/CONFIGURATION.zh.md](docs/CONFIGURATION.zh.md) |
| 平台对外配置（对外基址 / OAuth / 机器人凭据） | [docs/CONFIGURATION.zh.md](docs/CONFIGURATION.zh.md) |
| 上线部署（启动方式 / 数据库 / 配置清单） | [docs/DEPLOYMENT.zh.md](docs/DEPLOYMENT.zh.md) |

## 更新与维护

```bash
cd grape
git pull --ff-only                  # 拉取最新代码
pip install -r requirements.txt     # 依赖有变化时重装
```

**国内网络**：源码改用 GitCode 镜像 `git clone https://gitcode.com/grape-dev/grape.git`；pip 依赖加国内镜像源。
