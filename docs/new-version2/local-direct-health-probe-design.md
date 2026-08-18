# 本机直连引擎真实健康探测方案（对齐远端 worker，方案 C）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 目标：本机直连（credential_id=0）引擎像远端 worker 一样**真实探测**（跑 `--version`），使 `agent_runtimes.healthy` 准确，页面/执行/展示口径统一可信。

## 1. 现状与问题

| 探测函数 | 是否算 healthy | 运行处 |
|---|---|---|
| `discover_backends`（`src/agent/executor.py:61`） | ❌ 只 `shutil.which(cli)`（installed） | **API 服务器**（settings 刷新） |
| `_discover_local_runtimes`（`src/runtime/agent.py:35`） | ✅ `[cli, "--version"]` + `timeout=5`，`returncode==0` → healthy | 仅 **worker 进程** |

- 本机二进制（opencode/hermes）就装在服务器上，与远端 worker 探测其主机 CLI **同构**——技术上完全可探测，当前只是**没接上**。
- 后果：settings 刷新用 `discover_backends`（无 healthy）→ `sync_agent_runtimes` 把本机行 healthy **写 0**；而 Chat Agent 页 `_backends` 只看 `installed` → 页面显示、执行（`installed AND healthy`）拒绝。

## 2. 目标

1. 本机直连 healthy = **真实探测结果**（`--version` 成功），与远端一致、准确、可依赖。
2. 页面/执行/展示统一口径：本机直连 `installed AND healthy`（不再有"页面有、执行无"）。
3. 探测安全：超时 + 线程防护，不阻塞事件循环、不因 CLI 卡住拖垮请求。

## 3. 设计

### 3.1 探测函数（抽取公共，供服务器与 worker 复用）
新增 `src/runtime/local_probe.py`（避免服务器直接依赖 worker 模块 `agent.py`）：

```python
LOCAL_PROBE_TIMEOUT = 5          # 单引擎 `--version` 超时（秒）——与 worker 现有实现（agent.py:46）一致
LOCAL_PROBE_MAX_TOTAL = 25       # 全量探测总上限（秒）：5s×5 引擎留余量

def probe_local_engines(engines=None) -> list[dict]:
    """探测本机已安装引擎（跑 `--version`），返回 [{name, version, healthy, cli_path, installed}]。
    在独立线程执行（ThreadPoolExecutor），每引擎 5s 超时，总时长受限（MAX_TOTAL）。"""
    # engines 默认 = discover_backends() 结果（含 cli_path/installed）
    # 对每个 cli_path 在 executor 线程跑 [cli, "--version"]，timeout=LOCAL_PROBE_TIMEOUT
    # returncode==0 → healthy=True + 取 stdout 首行作 version；异常/超时 → healthy=False
```

> **超时取值**：`--version` 是平凡子命令，健康 CLI 毫秒级返回，**5s 已充裕**（10s 只兜底慢启动极端，且会减少总上限下可探测引擎数）；与 worker 现有实现共用同一常量，无漂移。**总时长上限是关键**——`MAX_TOTAL` ≥ 引擎数 × 单引擎超时，否则后面的引擎被截断、healthy 保持旧值/0。

- 逻辑 = 复用 `_discover_local_runtimes`（`agent.py:35-53`）的核心，抽到公共模块并加线程/总时长兜底。
- 服务器与 worker 都调它（worker 的 `_discover_local_runtimes` 改为转调，或保留别名）。

### 3.2 服务器侧接入（探测时机）
| 时机 | 变更 |
|---|---|
| **Agent Runtimes 刷新**（settings 刷新按钮） | 原 `discover_backends()` → `probe_local_engines()`（带 healthy/version），再 `sync_agent_runtimes` 写库 |
| **启动 seed / ensure_default_runtime** | 创建本机直连行时用探测结果写 healthy（不再默认 0） |
| **周期重探**（**间隔 10 分钟**） | sweeper 挂轻量定时：对 `credential_id=0` 行重探，防 healthy 过期（二进制变更/被删）；探测毫秒级、2~4 引擎，负载可忽略 |

### 3.3 健康语义（统一口径）
- 本机直连：`installed AND healthy`（保持），healthy 现在准确。
- **Chat Agent 页 `_backends`**（`chat.py:667`）：从只看 `installed` 改为 **`installed AND healthy=1`**——healthy 可信后，页面==执行，彻底消除"页面有、执行无"。
- `settings._runtime_health`（展示）：不变（`installed AND healthy`），现在能如实反映。

### 3.4 安全与并发
- 探测在 **ThreadPoolExecutor** 线程跑（不阻塞事件循环/请求线程）。
- 每引擎 5s 超时；全量总时长 ≤25s（超时终止剩余）。
- CLI 不存在（installed=False）→ 跳过探测，healthy=False。
- 探测异常（挂起/非零退出）→ healthy=False，不抛错。

## 4. 关键变更点

| 模块 | 变更 |
|---|---|
| `src/runtime/local_probe.py`（新） | `probe_local_engines()`（线程 + 超时 + 总时长限制） |
| `src/runtime/agent.py` | `_discover_local_runtimes` 转调 `local_probe`（或复用），保持 worker 行为 |
| `src/api/routes/settings.py` 刷新 | 改用 `probe_local_engines()` 后 `sync_agent_runtimes` |
| `src/user/db.py`（ensure_default_runtime / sync） | 用探测结果写 healthy |
| `src/api/routes/chat.py:667` `_backends` | 加 `AND healthy=1`（与执行一致） |
| 可选：周期重探 | sweeper 或轻量定时 |

## 5. 边界与风险

1. **探测开销**：刷新路径多几次 `--version`（毫秒~秒级，5s 超时兜底）；周期重探 10min 一次、频率低，可接受。
2. **CLI 卡住**：5s 超时 + 线程 + 总时长限制兜底，不拖垮请求。
3. **healthy 过期**：周期重探缓解（可选）；执行层遇 healthy=False 本就不选该引擎。
4. **与 B' 的关系**：本方案保持严格语义（installed AND healthy），healthy 现在准确；不采用"装了就能用"的放宽（避免掩盖 broken binary）。
5. **worker 复用**：抽公共模块避免 worker/API 双实现漂移。

## 6. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| P01 | `probe_local_engines` 对正常 CLI 返回 healthy=True + version | 本机有 opencode | healthy=True，version 非空（`--version` 首行） |
| P02 | 缺失 CLI → installed=False、跳过探测、healthy=False | PATH 无该 cli | healthy=False，不抛错 |
| P03 | CLI 挂起 → 5s 超时 → healthy=False | 假 CLI 卡住 | 探测在 ≤5s 内返回 healthy=False |
| P04 | 总时长限制：多引擎总探测 ≤ 上限 | 多引擎 | 整体 ≤ LOCAL_PROBE_MAX_TOTAL |
| P05 | 刷新后本机行 healthy 反映真实探测 | 刷新 | `agent_runtimes` 本机行 healthy=1（二进制在） |
| P06 | Chat Agent `_backends` 只列 installed+healthy | 一行 healthy=0 | 页面不再显示该行 |
| P07 | worker `_discover_local_runtimes` 行为不变（转调后同结果） | worker 环境 | 输出与旧实现一致 |
| P08 | 探测异常不抛错（非零退出 → healthy=False） | CLI 返回非零 | healthy=False，函数正常返回 |

## 7. 实施步骤

1. 新增 `src/runtime/local_probe.py`（线程 + 超时 + 总时长）。
2. `agent.py._discover_local_runtimes` 转调公共探测（保持 worker 行为）。
3. settings 刷新改用 `probe_local_engines()` → `sync_agent_runtimes`。
4. `ensure_default_runtime` / 启动 seed 用探测结果写 healthy。
5. Chat Agent `_backends` 加 `healthy=1`。
6. （可选）周期重探。
7. 测试 P01-P08 + 回归（settings / engine_select / chat / worker）→ 部署。

## 8. 结论

本机直连"不能真实探测"只是没接上：把远端 worker 已验证的 `--version` 探测机制抽成公共函数接到服务器侧刷新/启动，healthy 变准确，页面/执行/展示统一为 `installed AND healthy`，从根上消除"页面有、执行无"。
