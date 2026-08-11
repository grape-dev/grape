# 新增 Worker 接入指导

> 版本：v1.0
> 日期：2026-08-11
> 目标读者：需要给 Grape 集群新增一台可执行 Agent 的机器（worker）
> 关联：`runtime-admin-two-view-design.md`（Engines/Workers 管理）、`agent-runtime-design.md`（RuntimeAgent 边端）、`multi-runtime-instance-design.md`（多实例）

---

## 〇·一、一键安装（推荐）

生成凭据后，WebUI Workers 页会给出**一行命令**，在 Worker 机器上执行即可完成全部安装（拉取代码、Python 环境、venv、opencode 检测、systemd 服务并启动）：

```bash
curl -sSL https://grape.example.com/api/runtime/install | bash -s -- --token <rta_...>
```

可选参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--token` | 必填 | 凭据 token（rta_），WebUI 生成 |
| `--server` | `https://grape.example.com` | 服务端地址 |
| `--device` | `worker-<主机名>` | 设备名 |
| `--repo` | `https://gitcode.com/Jason7602/grape.git` | Grape 代码仓库（自建/镜像部署时改） |
| `--branch` | `new-version2` | 拉取分支 |

脚本自动：稀疏拉取（仅 `src/` + `skills`）→ 选择/安装 Python≥3.10 → 建 venv + 装 `requirements-worker.txt` → 检测/安装 opencode → 生成 systemd（token 隔离到 600 权限 env 文件）→ 启动服务。

> 幂等：重复执行安全；token 不暴露在 `systemctl cat` 可见的 service 文件中（在 `/etc/grape/runtime.env`，权限 600）。

**详细手工流程见下文**（一键安装失败或需定制时参考）。

---

## 〇、背景与原理

**Worker（设备）** = 一台安装了 `opencode` / `codearts` 等 CLI 的机器，通过 `runtime.agent` 进程与 Grape 服务端通信，领取并执行 DevOps Agent 任务（Issue 分析、代码实现、PR 审查等）。

```
GitCode/飞书/WebUI 事件 → Grape 服务端 (api.gitcode.com / grape.example.com)
        │  路由到 agent_tasks 队列（按 runtime 确定路由）
        ▼
Worker 设备（新机器）
  runtime.agent 进程：
    discover_runtimes → register → recover_orphans → 循环 claim → 执行 → report/complete
```

**通信方向：仅外连（outbound）**——worker 主动向服务端发 HTTPS 请求（register / heartbeat / claim / report），**无需服务端入站端口、无需公网 IP、可 NAT 后**。

**多 Worker 并存**：每台用唯一凭据（`rta_` token）注册，同名引擎（如 opencode）多行并存，互不覆盖 `cli_path/version`（`runtime_register` 按 `(credential_id, name)` 幂等 upsert）。

---

## 一、前提条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 服务端可达 | `https://grape.example.com`（HTTPS 443） | `curl https://grape.example.com/health` → `{"status":"ok"}` |
| opencode CLI | 必需，`opencode --version` 能跑通 | `opencode --version` |
| codearts CLI | 可选（如不装则只注册 opencode） | `codearts --version` |
| Python | `python3` ≥3.10（**推荐 3.12**）+ venv | `python3 --version`（<3.10 启动会报"requires Python >= 3.10"） |
| 系统 | Linux + systemd（推荐）；Windows 用进程守护（如 NSSM/任务计划） | — |
| Git | `git` 可用（coding/qa/sre 任务需要 worktree） | `git --version` |

> CLI 发现逻辑（`src/agent/executor.py:discover_backends`）：`opencode` / `codearts` / `hermes` / `cursor` 四个候选，`shutil.which` 命中即注册；`runtime.agent` 会用 `--version` 探测 healthy。

---

## 二、第一步：生成 Worker 凭据（WebUI）

1. 登录 Grape WebUI → 左侧 **Agent Runtimes** → **Workers 设备** 页签；
2. 点 **＋ 生成凭据**；
3. 填写：
   - **凭据名称**：全局唯一，建议 `worker-<短名>`（如 `worker-dev1`），可后续改名；
   - **范围**：个人（`user`，默认）/ 仓库（`repo`）/ 全局（`global`）；
     - **全局**：所有 DevOps Agent（vod/issue/coding/qa/sre）可绑定；
     - **仓库**：仅指定 `owner/repo` 的 DevOps Agent 可绑定；
     - **个人**：仅供 **Chat Agent** 使用，DevOps Agent 不可绑定；
   - 仓库范围需填 `repo_key`（`owner/repo`）。
4. 点 **生成** → 页面展示 **`rta_...` token（仅展示一次，请立即保存）** 与启动命令：

```
RUNTIME_TOKEN=rta_xxxx python -m runtime.agent
```

> ⚠️ **token 只显示一次**，服务端 DB 只存 sha256 哈希（`settings.py:398`）。丢失无法找回，只能吊销重新生成。

---

## 三、第二步：部署 Grape 代码与依赖（worker 机）

```bash
# 1. 拉取 Grape 代码（与生产版本一致）
git clone https://gitcode.com/Jason7602/grape.git /opt/grape
cd /opt/grape
git checkout new-version2          # 或生产分支

# 2. 建 venv 并安装依赖（worker 只需最小集 requirements-worker.txt）
# 先确认 python ≥3.10；若系统无合适版本，一键脚本会自动安装，手工流程可自行 apt/yum 安装 python3.12
python3.12 -m venv .venv        # 必须 ≥3.10；推荐显式 python3.12，避免默认 python3 是旧版
.venv/bin/pip install -r requirements-worker.txt   # 只含 aiohttp

# 3. 安装 opencode CLI（按项目要求；通常 npm 全局）
npm install -g opencode-ai
opencode --version                            # 验证

# 4. 验证 runtime.agent 可导入（不启动，仅冒烟）
.venv/bin/python -c "import src.runtime.agent; print('ok')"
```

> ✅ **worker 不需要 `requirements.txt`**（fastapi/uvicorn/paramiko 等是服务端用的）。端侧用独立的 **`requirements-worker.txt`**（只含 `aiohttp`）——`runtime.agent` 及其执行链（`src/runtime/agent.py` → `src/runtime/executor.py` → `src/runtime/opencode.py` → `src/config.py`）**只用 Python 标准库 + `aiohttp`**。

---

## 四、第三步：配置并启动 runtime-agent（systemd）

### 4.1 创建 service 文件

用 `tee` 一次性写入（替换 `<短名>` 与 `<rta_...>` 占位符）：

```bash
cat > /etc/systemd/system/runtime-agent.service << 'EOF'
[Unit]
Description=Grape RuntimeAgent (edge, worker-<短名>)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/grape
ExecStart=/opt/grape/.venv/bin/python -m src.runtime.agent
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=RUNTIME_TOKEN=<rta_刚生成的token>
Environment=RUNTIME_SERVER_URL=https://grape.example.com
Environment=RUNTIME_DEVICE_NAME=worker-<短名>
Environment=RUNTIME_MAX_CONCURRENT_TASKS=20
Environment=RUNTIME_POLL_INTERVAL=3
Environment=RUNTIME_HEARTBEAT_INTERVAL=15

[Install]
WantedBy=multi-user.target
EOF
```

### 4.2 环境变量在哪里修改

所有 worker 运行时配置都是 **`Environment=` 行**（在 `[Service]` 段），按需编辑：

| 方式 | 命令 | 适用 |
|---|---|---|
| **直接编辑文件** | `vim /etc/systemd/system/runtime-agent.service` → 改 `Environment=` 行 → 保存 | 全量修改（推荐，字段多时清晰） |
| **覆盖式编辑（不改原文件）** | `systemctl edit runtime-agent` → 在 `[Service]` 段写 `Environment=XXX=值` | 只改 1-2 个变量、保留默认 |

> ⚠️ **改完必须生效**：`systemctl daemon-reload && systemctl restart runtime-agent`，否则不生效。

### 4.3 启动 / 重启 / 查看

```bash
# 启动并开机自启
systemctl daemon-reload
systemctl enable --now runtime-agent

# 查看状态与错误
systemctl status runtime-agent
journalctl -u runtime-agent -f          # 跟踪日志

# 修改环境变量后重启
systemctl daemon-reload
systemctl restart runtime-agent

# 停止 / 禁用
systemctl stop runtime-agent
systemctl disable runtime-agent
```

### 4.4 不用 systemd 的替代（临时/容器）

想先验证不落地 systemd，可直接前台跑：

```bash
cd /opt/grape
RUNTIME_TOKEN=<rta_...> \
RUNTIME_SERVER_URL=https://grape.example.com \
RUNTIME_DEVICE_NAME=worker-<短名> \
.venv/bin/python -m src.runtime.agent
```

> 环境变量用 `KEY=VALUE` 前缀注入；Ctrl+C 停止。确认工作后再转 systemd 常驻。

### 4.5 环境变量说明

各变量含义（`src/runtime/agent.py:181-186`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RUNTIME_TOKEN` | 无（必填，缺则退出） | 凭据 token（`rta_`），对应服务端 runtime_credentials |
| `RUNTIME_SERVER_URL` | `http://localhost:3000` | **生产填 `https://grape.example.com`**；域名变更需同步每台 worker 此值 |
| `RUNTIME_DEVICE_NAME` | 主机名 | 设备名，用于 Engines/Workers 页显示 |
| `RUNTIME_MAX_CONCURRENT_TASKS` | 20 | 本机并发执行 Agent 数上限 |
| `RUNTIME_POLL_INTERVAL` | 3 | claim 轮询间隔（秒） |
| `RUNTIME_HEARTBEAT_INTERVAL` | 15 | 心跳间隔（秒） |

> 💡 **`RUNTIME_DEVICE_NAME` 与凭据名（`rta_` 名称）是两个独立字段，不必一致，但建议相关以便辨识**：
> - **凭据名**（WebUI 生成时填的 `worker-<短名>`）：`runtime_credentials.name`，**全局唯一**，用于 Workers 页识别该凭据/worker；
> - **`RUNTIME_DEVICE_NAME`**：worker 注册时回填到 `runtime_credentials.device_name`（`runtime.py:112`），并用于派生**引擎实例名** `instance_name = {type}-{device_suffix}`（如 `opencode-worker-dev1`，`instances.py:16`）；
> - 建议两者一致（如都用 `worker-dev1`），这样 Workers 页凭据名 与 Engines 页引擎实例名都能对得上；不一致也可运行，只是辨识上多一步。

---

## 五、第四步：验证接入

1. **worker 日志确认注册成功**：

```bash
journalctl -u runtime-agent -f
# 期望看到：
# INFO RuntimeAgent registered: [1, 5] -> https://grape.example.com, poll=3s heartbeat=15s max_concurrent=20
```

2. **WebUI 确认**：
   - **Workers 设备** 页 → 新 worker 心跳显示「在线 · Xs 前」；
   - **Engines 引擎** 页 → 出现该 worker 注册的引擎实例（`instance_name` = `{type}-{device短名}`，scope 按凭据范围）。

3. **功能测试**：在该 worker 范围可及的仓库发一个 Issue 事件（或评论触发 vod）→ 观察 worker 日志出现 `[seq=NNN] ▶ Step...` 执行、最终 `complete`；WebUI 事件页该 seq 状态 completed。

---

## 六、范围与可见性（关键）

| 凭据范围 | 谁可绑定 | 说明 |
|---|---|---|
| 全局（global） | 所有 DevOps Agent | 仓库/全局引擎，DevOps Agent 配置页可选 |
| 仓库（repo） | 仅指定仓库的 DevOps Agent | 对应该仓库的 engine |
| 个人（user） | 仅 Chat Agent | **DevOps Agent 不可选个人引擎**（`agents.py` 保存时 400 拒绝） |

- **DevOps Agent 绑定**：Agent 配置页 Backend 下拉 → 选 `{type}-{device}` 唯一引擎名，保存即绑定该 worker 的 `runtime_id`；
- **多 worker 同引擎**：多台 worker 都装 opencode → Engines 页多行 `opencode-*`，各自绑定/claim，互不覆盖。

---

## 七、常见问题排查

| 现象 | 排查 |
|---|---|
| 启动报 `TypeError: unsupported operand type(s) for \|: 'type' and 'NoneType'` 或 `requires Python >= 3.10` | **Python 版本过旧**（<3.10，代码用 `X \| None` 语法）。用 `python3.12 -m venv .venv` 重建 venv 并重装依赖 |
| `Register failed (check RUNTIME_TOKEN / server URL)` | ① token 是否有效（WebUI Workers 页是否 active）；② `curl https://grape.example.com/health` 通不通；③ `RUNTIME_SERVER_URL` 是否带 `https://` |
| 401/403 注册被拒 | token 已被吊销（WebUI 吊销后旧 token 失效）→ 重新生成 |
| 有 queued 任务但 worker 不执行 | 确认任务 runtime 路由指向该 worker；确认 worker 不在串行槽阻塞（同 session 活跃任务） |
| 任务卡 `dispatched` 不跑 | worker 事件循环阻塞 / 长命令静默超时（见 `worker-event-loop-block-rootcause.md` / `long-command-silent-timeout-fix.md`） |
| opencode 引擎未注册 | `opencode` 不在 PATH；`discover_backends` 只认 `shutil.which` 命中的 4 个候选 |
| 升级 | `cd /opt/grape && git pull && systemctl restart runtime-agent` |
| 移除 worker | WebUI Workers 页「吊销」该凭据 → 其引擎标记 offline，后续 claim 被拒 |

---

## 八、参考

- `runtime-admin-two-view-design.md`：Engines/Workers 双视图、scope/owner、凭据管理
- `agent-runtime-design.md`：RuntimeAgent 边端、协议（register/heartbeat/claim/report）
- `multi-runtime-instance-design.md`：多实例键、串行槽、S2 健康选择
- `skill-delivery-remote.md`：技能/项目基线随任务下发（worker 无需预装技能）
- `worker-event-loop-block-rootcause.md` / `long-command-silent-timeout-fix.md`：任务卡 dispatched / 长命令超时排查
