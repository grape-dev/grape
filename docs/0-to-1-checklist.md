# 0→1 安装自检清单（0-to-1 Installation Checklist）

> 面向按官方 README 自部署的开源用户：在干净 Linux 主机上从 0 到 1 跑通
> 「拉取 → 启动 → OAuth 登录 → 托管 → 首个任务」，并自助定位失败点。
> 对应 Issue #455 实测卡点：按阶段逐项核对「操作 / 期望结果 / 失败排查」。

---

## 阶段 0 · 环境准备

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| Python 版本 | `python3 --version` | `>= 3.10`（如 3.10.x / 3.11.x / 3.12.x） | 低于 3.10 升级：`apt install python3.12` 等；启动时会有显式提示 |
| Node.js（Agent CLI 可选） | `node --version` | `>= 18` | 不装 Node 时可用 Python 版 CLI |
| Git | `git --version` | 任意较新版本 | 安装 git |

## 阶段 1 · 拉取代码与依赖

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| 拉取仓库 | `git clone https://github.com/grape-dev/grape.git && cd grape` | 仓库文件齐全 | 网络抖动时重试；或配置代理/镜像（见 README「国内镜像加速」与下方「网络建议」） |
| 创建虚拟环境 | `python3 -m venv .venv && source .venv/bin/activate` | venv 创建成功 | 确认 Python >= 3.10 |
| 安装依赖 | `pip install -r requirements-local.txt`（最小集）/ `-r requirements.txt`（完整） | 无报错 | 网络慢：`-i https://mirrors.aliyun.com/pypi/simple/`；或 `pip config set global.index-url` |

> 网络建议：git 拉取偶发 HTTP/2 / 连接超时属正常抖动，平台已做有限次自动重试（3 次退避）。
> 仍失败可降级 HTTP/1.1：`git config --global http.version HTTP/1.1`；或配置镜像/代理（如
> `git config --global url."https://ghproxy.com/https://github.com/".insteadOf "https://github.com/"`，
> 请按你的网络环境选择可信代理）。

## 阶段 2 · 配置（必做三项）

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| .env | `cp .env.example .env` | 文件存在 | — |
| **对外基址** | `cp configs/server-config.example.json configs/server-config.json`，填写 `server_base_url` / `webhook_url` 为**对外可访问**地址 | `/api/health` 返回 `"server_base_url_configured": true` | 未配置时启动日志出现 `[CONFIG] 未配置对外可访问基址` 告警、健康检查返回 `server_base_url_warning`；OAuth 登录前必须配置，否则回调指向 localhost 第三方无法回访 |
| **OAuth 凭据** | `cp configs/oauth-providers.example.json configs/oauth-providers.json`，填写 GitCode / GitHub 的 `client_id` / `client_secret`，并保持 `redirect_uri` 与 OAuth App 回调地址一致 | 重启后 `/api/auth/login/gitcode` / `/api/auth/login/github` 返回 200 与跳转地址 | 返回 400（not_configured）= 凭据文件/字段未配对；确认写入 `configs/oauth-providers.json` 而非其它文件 |

## 阶段 3 · 启动

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| 启动服务 | `python -m src.api.run --dev`（开发）或 `python -m src.api.run`（生产双进程） | 日志出现 API 启动成功；无版本报错 | Python < 3.10 会显式提示并退出——升级后重试 |
| 健康检查 | `curl -s http://localhost:3000/api/health` | `{"server":"ok", ...}` | 服务未启动：查看日志 |
| 基址告警 | 查看启动日志 | 无 `[CONFIG]` WARNING（或已按阶段 2 配置） | 按 WARNING 文案配置 `configs/server-config.json` |

## 阶段 4 · OAuth 登录

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| GitCode 登录 | 打开 Web UI → 登录 → GitCode OAuth 授权 | 登录成功进入工作台 | 400 not_configured → 检查 `oauth-providers.json`；回调失败 → 检查 `server_base_url` / `redirect_uri` |
| GitHub 登录（可选） | 登录 → GitHub OAuth 授权 | 登录成功 / 账号自动创建 | 同上；登录后可在「账号设置」查看第三方身份绑定（确认落库） |

> 数据一致性说明：GitHub OAuth 自动创建账号后平台会执行「读回校验」，若账号/绑定未持久化会返回
> 失败而非静默登录（详见服务端 ERROR 日志 `OAuth auto-create READBACK FAILED`）。

## 阶段 5 · 托管第一个仓库

| 检查项 | 操作 | 期望结果 | 失败排查 |
|--------|------|----------|----------|
| 托管仓库 | 工作台 → 仓库列表 → 加入并托管 | 提示「托管成功」，仓库转为已托管 | 403 权限提示 → 使用仓库**所有者**的凭据，或授予机器人管理员权限；502 上游错误 → 稍后重试，或使用「修复托管」自愈 |
| 首次任务 | 进入仓库工作台 → 下发一个 Issue/任务 | Agent 开始执行，任务卡片出现 | 查看服务端日志定位 |

## 常见命令速查

```bash
python scripts/doctor.py          # 安装前自检（Python 版本/依赖/配置/端口/DB）
curl -s http://localhost:3000/api/health   # 健康检查（含基址配置状态）
```

## 自检结果记录（建议截图/复制保存）

```
Python 版本:     ___
git clone:       成功 / 失败
pip install:     成功 / 失败
server_base_url: 已配置 / 未配置
oauth-providers: 已配置 / 未配置
服务启动:        成功 / 失败
OAuth 登录:      GitCode ___  GitHub ___
仓库托管:        成功 / 失败(403/502/其他)
首个任务:        成功 / 失败
```