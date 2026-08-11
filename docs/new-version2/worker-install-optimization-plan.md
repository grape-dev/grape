# 端侧（Worker）安装优化方案

> 版本：v1.0
> 日期：2026-08-11
> 状态：待评审
> 前置：`add-new-worker-guide.md`（当前手工安装流程）、`agent-runtime-design.md`（RuntimeAgent 边端）
> 目标：将端侧安装从 7 步手工压缩为 **1 步（复制一行命令执行）**，并解决 token 明文、全仓拉取、多步易错问题。

---

## 〇、当前安装流程与痛点

### 现状（add-new-worker-guide.md）

```
1. WebUI 生成凭据 → 手动复制 rta_ token
2. git clone 整个 grape 仓库（含服务端全部代码）
3. python3 -m venv .venv
4. .venv/bin/pip install -r requirements-worker.txt
5. npm install -g opencode-ai
6. 手写 /etc/systemd/system/runtime-agent.service（token 填进 Environment）
7. systemctl daemon-reload && enable --now
```

### 痛点

| # | 痛点 | 影响 |
|---|---|---|
| 1 | **clone 整个仓库** | worker 只需 `src/runtime/*` 与少量模块；全仓含服务端代码 + configs，体积大、拉取慢、含敏感配置 |
| 2 | **token 手工复制粘贴** | 易错、易泄漏；明文写进 systemd `Environment=` |
| 3 | **多步手工** | venv/pip/npm/systemd 分开做，易遗漏、出错难定位 |
| 4 | **opencode 安装手动** | 依赖 npm 全局安装，不同系统命令不一 |

---

## 一、方案 A（核心）：一键安装脚本 `scripts/install-worker.sh`

参数化运行，一步完成：

```bash
curl -sSL <脚本URL> | bash -s \
  --token <rta_...> \
  --server https://grape.example.com \
  --device worker-dev1
```

### 脚本职责

1. **稀疏拉取仓库**（避免全仓 + 敏感暴露）：
   ```bash
   git clone --filter=blob:none --sparse <repo_url> /opt/grape
   cd /opt/grape
   git sparse-checkout set src/runtime src/config.py src/repo_key.py src/session_manager.py skills
   ```
   - worker 运行仅需这些路径；`configs/`、`data/`、`docs/` 等不拉取；
   - 后续 `git pull` 保持 sparse 子集。

2. **建 venv + 最小依赖**：
   ```bash
   python3.12 -m venv .venv          # 必须 ≥3.10（代码用 X | None 语法）；推荐显式 3.12
   .venv/bin/pip install -r requirements-worker.txt
   ```

3. **检测/安装 opencode**：
   - `command -v opencode` 存在则跳过；
   - 缺失则 `npm install -g opencode-ai`（或按 OS 提示）。

4. **生成 systemd 文件**（参数填 Environment，见方案 C 的 EnvironmentFile 变体）：
   ```bash
   cat > /etc/systemd/system/runtime-agent.service << EOF
   [Unit]
   Description=Grape RuntimeAgent (edge, ${DEVICE})
   ...
   Environment=RUNTIME_TOKEN=${TOKEN}
   Environment=RUNTIME_SERVER_URL=${SERVER}
   Environment=RUNTIME_DEVICE_NAME=${DEVICE}
   ...
   EOF
   ```

5. **启动**：
   ```bash
   systemctl daemon-reload
   systemctl enable --now runtime-agent
   ```

### 脚本设计要点

- **幂等**：重复执行安全（已存在目录/venv/service 则复用）；
- **分步输出**：每步成功/失败明确提示，失败不静默继续；
- **token 来源**：命令行参数 `--token` 或环境变量 `RUNTIME_TOKEN`，**不硬编码进脚本文件**；
- **参数校验**：token 必填、server 默认 `https://grape.example.com`、device 默认主机名。

---

## 二、方案 B（辅助）：WebUI 一键命令下发

生成凭据后，WebUI 直接展示**可复制的一行命令**（token 由调用方注入，不写 URL/日志）：

```
在 worker 上执行：
curl -sSL https://grape.example.com/api/runtime/install | TOKEN=<rta_...> bash
```

### 实现

- **服务端端点** `GET /api/runtime/install`：返回 `scripts/install-worker.sh` 内容（只读，无敏感信息；token 由 worker 侧以 `TOKEN` 环境变量注入）；
- **前端**：生成凭据成功弹窗内，除了当前"复制 token + 启动命令"，额外展示该一键命令（token 拼接进 `TOKEN=`）；
- token 仍只在 worker 执行时存在于进程环境，不落日志/URL。

### 说明

- 若不愿暴露 install 端点，前端可直接拼接：
  ```
  curl -sSL <raw脚本URL> | bash -s -- --token <rta_> --server https://grape.example.com --device <短名>
  ```
  （token 作为命令行参数同样不落 URL/日志。）

---

## 三、方案 C（安全）：systemd 凭据隔离（EnvironmentFile）

- 脚本生成 systemd 时，**token 写入独立 `EnvironmentFile`**，权限 600，仅 root 可读：
  ```bash
  mkdir -p /etc/grape
  cat > /etc/grape/runtime.env << EOF   # chmod 600
  RUNTIME_TOKEN=<rta_...>
  EOF
  ```
  ```ini
  # /etc/systemd/system/runtime-agent.service
  [Service]
  EnvironmentFile=/etc/grape/runtime.env
  Environment=RUNTIME_SERVER_URL=https://grape.example.com
  Environment=RUNTIME_DEVICE_NAME=worker-dev1
  ...
  ```
- 避免 token 明文直接暴露在 `systemctl cat runtime-agent` 可读的 service 文件中；
- 其余非敏感参数（server/device/并发）仍放 service `Environment=`。

---

## 四、方案 D（后续可选）：worker 独立分发

- 把 `src/runtime/*` + `src/config.py` + `src/repo_key.py` + `src/session_manager.py` + 最小依赖抽成独立安装包 `grape-worker`；
- `pip install grape-worker` + 一条启动命令即可，无需 clone 仓库；
- 本期不做，作为后续演进（依赖本仓库结构稳定后）。

---

## 五、推荐组合与落地顺序

**A + B + C**：

| 步骤 | 内容 |
|---|---|
| 1 | 新增 `scripts/install-worker.sh`（方案 A：稀疏拉取 + venv + opencode + systemd） |
| 2 | systemd 生成用 EnvironmentFile 隔离 token（方案 C） |
| 3 | 新增 `GET /api/runtime/install` 返回脚本 + WebUI 弹窗展示一键命令（方案 B） |
| 4 | 更新 `add-new-worker-guide.md`：主流程改为一键命令，手工流程降为附录 |

**效果**：端侧安装从 7 步 → **1 步**（复制一行命令执行）；token 不再明文暴露；不再拉取全仓。

---

## 六、验证

1. 干净 Linux 机器执行一键命令 → worker 上线、Engines/Workers 页可见、可领取任务；
2. 重复执行 → 幂等不报错；
3. `systemctl cat runtime-agent` 不含 token（token 在 600 权限 env 文件）；
4. 稀疏检出后 `git pull` 正常（仅更新 worker 子集）。

---

## 七、改动点清单

| # | 文件 | 改动 | 方案 |
|---|---|---|---|
| 1 | `scripts/install-worker.sh`（新增） | 一键安装：稀疏拉取 + venv + opencode + systemd(EnvironmentFile) | A + C |
| 2 | `src/api/routes/runtime.py` | `GET /api/runtime/install` 返回脚本内容（只读） | B |
| 3 | `src/api/static/js/pages/runtimes.js` | 生成凭据弹窗追加一键命令展示 | B |
| 4 | `docs/new-version2/add-new-worker-guide.md` | 主流程改一键命令 + 手工附录 | 文档 |
| 5 | 新增 `docs/new-version2/worker-install-optimization-plan.md`（本文） | 方案记录 | 文档 |

*本文为优化方案，待评审后实施。*
