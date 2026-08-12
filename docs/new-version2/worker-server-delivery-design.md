# Worker 端侧交付优化方案（服务端下发运行包）

> 版本：v1.1
> 日期：2026-08-12
> 状态：已评审确认，待实施
> 前置：`worker-install-optimization-plan.md`（一键安装脚本）、`agent-runtime-design.md`（RuntimeAgent 边端）、`add-new-worker-guide.md`（当前手工安装流程）
> 目标：**去除 `git clone` 依赖**，改由 grape 服务端用 `rta_` 凭据鉴权后直接下发 worker 运行包，实现真正的"一行命令一键安装"。

---

## 〇、背景与问题

当前 `scripts/install-worker.sh`（对应 `worker-install-optimization-plan.md` 方案 A/B/C）已实现：

```bash
curl -sSL <server>/api/runtime/install | bash -s -- --token <rta_...>
```

其中 `GET /api/runtime/install`（`src/api/routes/runtime.py:312`）返回脚本内容，前端 Workers 页弹窗（`runtimes.js:173`）展示上述一行命令。

但脚本内部（`install-worker.sh:81-95`）仍通过 `git clone --filter=blob:none --sparse` 从代码仓库
`https://gitcode.com/Jason7602/grape.git` 拉取 `src skills requirements-worker.txt`，存在以下问题：

| # | 问题 | 影响 |
|---|---|---|
| 1 | **worker 代码从 GitCode 仓库拉取** | 客户端 VM 必须能访问 GitCode 外部网络；`new-version2` 分支 HEAD 可能与线上部署版本不一致 → **worker 与服务端协议漂移** |
| 2 | **拉取范围过大** | 实际运行只需 14 个模块（~175KB），却拉取整个 `src/`（162 文件 / 4.4MB）+ `skills/`（170 文件 / 0.4MB）+ git 元数据；90% 为 worker 永不执行的代码（`src/api/*` 等），且含服务端侧代码与敏感配置结构 |
| 3 | **客户端需安装 git** | 新增前置依赖，私有/内网场景不便 |
| 4 | **无版本对齐机制** | 服务端与 worker 代码来源不同步，协议变更时易出隐性故障 |

> 用户原始反馈（`install-worker.sh:81`、`user-workflow-overview.md:779`）：
> "不应该从代码仓库拉取代码，而应该 grape server 端提供一个接口，到客户端的虚机上，
> 通过获取的证书 token，能够自动下载安装客户端所依赖的包和工具；然后实现一键安装，这样的体验会更好。"

---

## 一、worker 运行时依赖闭包分析（本方案依据）

对 `src.runtime.agent` 入口做 import 闭包（含懒加载）分析：

| 项 | 量 |
|---|---|
| 实际运行模块 | **14 个**：`src/runtime/*`、`src/agent/executor.py`、`src/config.py`、`src/repo_key.py`、`src/session_manager.py`、`src/user/db.py`、`src/mysql_compat.py`、`src/api/routing.py` |
| 代码体积 | ~175 KB |
| 第三方依赖 | 仅 `aiohttp`（`requirements-worker.txt` 已最小化） |
| 技能 | 任务 payload 已带 `skill_files`（worker 落到 `cwd/.skills`），本地 `skills/` **非必需** |
| 执行引擎 | **不随包下发**：opencode / codearts 等由用户自行安装，worker 启动时 `discover_runtimes` 自动发现并注册（引擎类型不限定 opencode） |

结论：完全可由服务端按清单打包下发，无需 git；执行引擎与 worker 解耦，安装流程不涉任何引擎。

---

## 二、方案设计

### 2.1 新增服务端包下发接口

```
GET /api/runtime/install/package
鉴权：Authorization: Bearer rta_<token>（复用 _auth_credential，与 register/claim 一致）
响应：application/gzip 的 tar.gz 包
```

包内容：

```
grape-worker/
├── src/                      # 闭包代码（按清单打包）
│   ├── runtime/              # 整目录全量包含（防懒加载遗漏）
│   ├── agent/executor.py
│   ├── config.py
│   ├── repo_key.py
│   ├── session_manager.py
│   ├── api/routing.py
│   └── user/db.py
├── requirements-worker.txt   # aiohttp
├── VERSION                   # 部署 commit / 包版本号
└── SHA256SUMS                # 包内容校验
```

**打包方式**：**首次请求时按 manifest 清单动态打包，产物写入临时文件缓存**，后续请求直接命中缓存文件返回（不落内存）。

- **缓存路径**：`data/cache/worker-package/grape-worker.tar.gz`（`data/` 为服务端 gitignore 运行时目录，不随代码仓库分发）；同目录放 `VERSION`（当前包版本），供 `/latest` 接口读取。
- **缓存失效**：服务端代码变更（部署/重启）后自动重新打包。实现上以**代码目录 mtime / manifest 内容 hash** 为缓存键——请求时比对当前代码状态与缓存 `VERSION`，不一致则重建并覆盖缓存文件。
- **并发**：多 worker 同时首次请求时加锁（进程内锁 + 写临时文件 `*.tmp` 后原子 rename），避免重复打包/读到半包。

manifest 建议**以整目录粒度包含 `src/runtime/`**（含运行时才 import 的 `repogit.py`、`cleanup.py` 等），
避免手工维护 import 图导致漏包。

### 2.2 改造安装脚本 `scripts/install-worker.sh`

一键命令保持一行不变：

```bash
curl -sSL <server>/api/runtime/install | bash -s -- --token <rta_...> [--server <url>] [--device <name>]
```

脚本内部流程改为：

1. 用 `--token` 调 `GET /api/runtime/install/package`（`Authorization: Bearer`）下载 tar.gz，校验 `SHA256SUMS`；
2. 解压到安装目录 `/opt/grape`（沿用现有路径，**不再是 git 仓库**，仅存放 worker 运行包）；
3. 准备 Python ≥ 3.10（现有 `pick_python` 逻辑保留：探测 / apt / yum 自动安装）；
4. `python -m venv` + `pip install -r requirements-worker.txt`；
5. 写 systemd（`EnvironmentFile` 存 token，权限 600，保留方案 C 隔离）；
6. `systemctl daemon-reload && enable --now`。

> **不安装任何执行引擎**：opencode / codearts 等由用户后续自行安装（见 2.3），
> worker 启动时通过 `_discover_local_runtimes()` 自动发现本机已装引擎并注册到服务端；
> 未装任何引擎时照常上线（注册空 runtimes，UI 提示"无可用引擎"）。

**向后兼容**：脚本检测到旧安装目录存在 `.git`（git 方式安装的存量 worker）时，打印迁移提示
或走旧 `--repo` 回退分支；生产现有 worker（123.249.12.67）不受影响。

### 2.3 执行引擎安装（不在一键安装环节内）

**决策：安装脚本不安装任何执行引擎（opencode 等）**。理由：

- 引擎类型**不限定 opencode**（可能 codearts / 其他 CLI），由用户按需选用；
- 引擎与 worker 解耦：worker 只负责从服务端领任务 + 调用本机已装引擎执行；
- 一键安装只管"把 worker 跑起来"，引擎安装留给用户按环境自行处理，降低脚本耦合与失败点。

后续用户自行安装引擎后，worker 重启（或新 worker 注册）时经 `discover_runtimes` 自动发现并上报，
Engines/Workers 页即显示该引擎（含版本/健康态）。脚本可提供**安装指引提示**（非自动执行）：

```
检测到未安装任何执行引擎。请自行安装后重启 worker：
  如 opencode: npm install -g opencode-ai   （或按官方文档）
完成后 systemctl restart runtime-agent 即可自动注册该引擎。
```

### 2.4 版本与更新机制（本期实现）

- 包内嵌 `VERSION`（部署 commit 或语义版本）；
- 服务端提供 `GET /api/runtime/install/latest`（rta_ 鉴权）返回当前包版本号；
- **worker 启动时自检**：向 `/latest` 查询版本，与本机 `VERSION` 不一致则重新下载包并原地替换
  （或提示执行一键命令升级），保证服务端与 worker 代码随部署自动对齐；
- 脚本安装完成后写 `/etc/grape/worker.version`，供后续自检对比。

### 2.5 安全

- 包下载接口强制 `rta_` 鉴权（复用 `_auth_credential`），未授权返回 401；
- 脚本本身（`/api/runtime/install`）无敏感信息，可保持公开；
- 包内不含 `src/api/*` 服务端代码、`configs/`、`data/`，也不含任何执行引擎二进制；
- `SHA256SUMS` 校验防篡改。

---

## 三、改动点清单

| # | 文件 | 改动 | 方案 |
|---|---|---|---|
| 1 | `scripts/install-worker.sh`（改造） | 下载服务端包解压安装（沿用 `/opt/grape`），替代 git clone；不装引擎，仅提示；保留 `--repo` 回退 | 2.2 |
| 2 | `src/api/routes/runtime.py` | 新增 `GET /api/runtime/install/package`（rta_ 鉴权，首次请求动态打包到 `data/cache/worker-package/` 后复用，返回 tar.gz） | 2.1 |
| 3 | `src/api/routes/runtime.py` | 新增 `GET /api/runtime/install/latest`（rta_ 鉴权，返回包版本号） | 2.4 |
| 4 | `src/runtime/agent.py` | 启动时自检 `/latest` 版本，不一致则提示/重新下载（本期实现版本对比 + 提示） | 2.4 |
| 5 | `src/api/static/js/pages/runtimes.js` | 弹窗命令不变（仍一行）；补充"引擎需自行安装"提示 | 2.2 |
| 6 | `docs/new-version2/add-new-worker-guide.md` | 主流程改为一键命令（服务端下发），git 手工流程降为附录 | 文档 |
| 7 | `docs/new-version2/worker-install-optimization-plan.md` | 补充本方案为演进方向 | 文档 |
| 8 | 新增 `docs/new-version2/worker-server-delivery-design.md`（本文） | 方案记录 | 文档 |

---

## 四、验证

1. 干净 Linux 机器（未装 git/opencode）执行一行命令 → 从服务端下载包 → worker 上线、Engines/Workers 页可见；
2. 断网/屏蔽 GitCode 的环境可完成安装（仅连 grape server）；
3. 重复执行 → 幂等不报错；
4. `systemctl cat runtime-agent` 不含 token（token 在 600 权限 env 文件）；
5. 包内容校验（SHA256SUMS）通过；
6. 未装引擎时 worker 照常上线（注册空 runtimes），脚本给出引擎安装指引；用户装 opencode 后
   `systemctl restart runtime-agent` → 引擎自动注册、可领任务；
7. 服务端发新版后，worker 启动自检 `/latest` 提示版本不一致（本期提示，自动替换为后续增强）。

---

## 五、已确认决策

| # | 决策项 | 结论 |
|---|---|---|
| 1 | 执行引擎（opencode 等） | **不进一键安装环节**，用户后续自行安装；引擎类型不限定 opencode |
| 2 | 安装目录 | 沿用 `/opt/grape`（非 git 仓库，仅存 worker 运行包） |
| 3 | 打包时机 | 首次请求动态打包 + 写入临时文件缓存，缓存路径 `data/cache/worker-package/`，代码变更后失效重建 |
| 4 | `latest` 版本接口 | 本期实现；worker 启动自检，本期先提示、自动替换留作后续增强 |

---

*本文为优化方案，已评审确认，待实施。*
