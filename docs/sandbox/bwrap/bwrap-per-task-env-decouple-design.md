# bwrap 沙箱 per-task 环境解耦方案（v5 最终版）

> 分支：feat/devbox-sandbox-integration
> 日期：2026-10-08
> 前置文档：docs/weekly-05/sandbox-architecture.md §3 + §14
> 审查历史：v2→v3→v4→v5（删 fallback + 删 GRAPE_SANDBOX_HOME + HOME 注入上移 + 外部审查 5 点确认）

## 一、问题背景

bwrap 沙箱 HOME 重构后，hermes 验证通过，但 codearts/opencode 并发执行失败：
```
bwrap: Can't mkdir /root/.cache/grape-sandbox/.codeartsdoer: Read-only file system
```

## 二、根因全景（8 个耦合点）

| # | 耦合点 | 根因 |
|---|--------|------|
| 1 | `GRAPE_SANDBOX_HOME` 三处设置值不一致 | `agent.py`/`executor.py` 写基础路径，`bwrap.py` 写 per-task 路径 |
| 2 | `GRAPE_SANDBOX_HOME` 沙箱内无消费方 | YAGNI——死代码留着误导，与 null 语义冲突 |
| 3 | codearts `prepare_task_env` 写 `os.environ` | 进程级全局，并发污染 opencode |
| 4 | bwrap.py `_build_env` 硬编码 `if hermes` | 违反开闭原则 |
| 5 | bwrap.py `GRAPE_TASK_RW_DIRS` 有 `os.environ` fallback | 全局污染入口 |
| 6 | hermes per-task 逻辑散在 `hermes_acp.py` | 绕过 `prepare_task_env` 钩子 |
| 7 | hermes_acp 有 fallback overlay 构建 | "悄无声息退回非隔离"的 bug 入口 |
| 8 | HOME 注入绑在 bwrap._build_env | null 降级时 per-task HOME 不注入，并发隔离失效 |

## 三、三维度正交设计

```
引擎维度：prepare_task_env(task_id, env)     ← 无条件执行，写 per-task env
协议维度：protocol.stream_execute(cmd, env)  ← 拿 cmd
沙箱维度：sandbox.wrap(argv, env)            ← 套壳/不套（bwrap/none 各自实现）
```

## 四、设计原则

1. `env["HOME"]` 注入在 `agent.py`（prepare_task_env 之前）— 不绑 bwrap，null 降级也生效
2. `prepare_task_env` 无条件执行 — per-task 隔离是引擎需求，不依赖沙箱后端
3. `Sandbox.prepare()` 各后端保证 HOME 可用 — bwrap makedirs 供 --bind，null makedirs 供真实路径
4. bwrap._build_env 只读 `env["HOME"]` — 不再写 HOME，只组装 --bind/--setenv
5. 所有 per-task 变量只通过 `env` dict 传递 — 不写 `os.environ`
6. bwrap.py 不感知引擎需求 — 删 if hermes 硬编码
7. 删掉 `GRAPE_SANDBOX_HOME` — 三处写入 + --setenv 白名单全删
8. 删掉 hermes_acp fallback — fail loud，不静默退回非隔离
9. `GRAPE_TASK_RW_DIRS` 在 null 模式下是 no-op（NullSandbox.wrap 不读它，恒等返回 cmd）

## 五、9 个改动点

### 改动 1：新增 `src/agent/sandbox/overlay.py`

把 `hermes_acp.py` 的 `_build_overlay_home` 迁移到这里，改名 `build_overlay_home`。
避免 `runtime.hermes → agent.hermes_acp → agent.sandbox.backends.bwrap` 依赖链。

### 改动 2：`src/runtime/base.py` — prepare_task_env docstring

补充契约：只写 env dict + 可调 `sandbox_home_for()`。

### 改动 3：`src/runtime/hermes.py` — 新增 prepare_task_env

```python
def prepare_task_env(self, task_id: str, env: dict, resource_stack=None) -> None:
    from src.agent.sandbox.backends.bwrap import sandbox_home_for
    from src.agent.sandbox.overlay import build_overlay_home
    iso_home = sandbox_home_for("hermes", task_id)
    os.makedirs(iso_home, exist_ok=True)
    src_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    build_overlay_home(iso_home, src_home)
    env["HERMES_HOME"] = iso_home
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
```

### 改动 4：`src/runtime/codearts.py` — per-task 路径 + 不污染全局

```python
# 改前
_sandbox_home = _os.environ.get("GRAPE_SANDBOX_HOME", "") or _home
_os.environ["GRAPE_TASK_RW_DIRS"] = _json_mod.dumps([...])
env["GRAPE_TASK_RW_DIRS"] = _os.environ["GRAPE_TASK_RW_DIRS"]

# 改后
from src.agent.sandbox.backends.bwrap import sandbox_home_for
_sandbox_home = sandbox_home_for("codearts", task_id)
_task_rw_dirs = _json_mod.dumps([...])
env["GRAPE_TASK_RW_DIRS"] = _task_rw_dirs  # 只写 env dict
```

### 改动 5：`src/runtime/agent.py` — HOME 注入上移 + 删全局污染

```python
# 改前（728-739 行）
_is_bwrap = _os.environ.get("GRAPE_AGENT_SANDBOX", "").lower() in (...)
if _is_bwrap:
    _os.environ["GRAPE_SANDBOX_HOME"] = _SANDBOX_HOME

# 改后
# 删掉整个 if 块
# 新增（在 prepare_task_env 之前）：
from src.agent.sandbox.backends.bwrap import sandbox_home_for
env["HOME"] = sandbox_home_for(engine_name, tid)
```

### 改动 6：`src/agent/executor.py` — 删 GRAPE_SANDBOX_HOME

删掉 645 行 `_proc_env["GRAPE_SANDBOX_HOME"] = _SANDBOX_HOME`。

### 改动 7：`src/agent/hermes_acp.py` — 从 env 读 + 删 fallback + 用 overlay.py

```python
# 改前（602-629 行）
from src.agent.sandbox.backends.bwrap import _sandbox_home
iso_home = _sandbox_home("hermes", _tid)
_build_overlay_home(iso_home, src_home)
proc_env["HERMES_HOME"] = iso_home

# 改后
iso_home = (env or {}).get("HERMES_HOME", "")
if not iso_home:
    raise RuntimeError(
        "HERMES_HOME missing in env; prepare_task_env must run before stream_execute"
    )
proc_env["HERMES_HOME"] = iso_home
```

同时：`_build_overlay_home` 和 `_cleanup_overlay` 从 hermes_acp.py 删除（已迁移到 overlay.py）。

### 改动 8：`src/agent/sandbox/backends/bwrap.py` — 公共化 + 去引擎化

1. `_sandbox_home` 改名 `sandbox_home_for` + `__all__` 导出 + docstring
2. `_build_env` 删掉 `env["HOME"] = ...`（HOME 由 agent.py 注入），只读 `env["HOME"]`
3. `_build_env` 删掉 `env["GRAPE_SANDBOX_HOME"] = ...`
4. `_build_env` 删掉 `if self._engine_type == "hermes": ...`
5. `GRAPE_TASK_RW_DIRS` 删掉 `os.environ` fallback
6. `--setenv` 白名单删掉 `GRAPE_SANDBOX_HOME`（保留 `HOME`）
7. 所有引用 `_sandbox_home` 的地方改名为 `sandbox_home_for`

### 改动 9：`src/agent/sandbox/backends/null.py` — __enter__ makedirs HOME

确认 `NullSandbox.__enter__` 里 `os.makedirs(env["HOME"])`（如果 agent.py 已 makedirs 则跳过）。

## 六、env 传递链确认

### 路径 A：opencode/codearts/codex（走 _run_once）

```
agent.py _execute_task
  ├── env["HOME"] = sandbox_home_for(engine, task_id)    ← HOME 注入（不绑 bwrap）
  ├── prepare_task_env(task_id, env)                      ← 引擎设 GRAPE_TASK_RW_DIRS
  ├── stream_runtime(env_extra=env)
  │   └── stream_agent → _run_once
  │       ├── _proc_env = env
  │       ├── sandbox = sandbox_manager.prepare(...)
  │       │   bwrap: __enter__ → makedirs(env["HOME"])
  │       │   null:  __enter__ → makedirs(env["HOME"])
  │       ├── sandbox.wrap(cmd, proc_env=_proc_env)
  │       │   bwrap: _build_env 读 env["HOME"] 组装 --bind/--setenv
  │       │   null:  恒等返回 cmd
  │       └── 引擎执行
```

### 路径 B：hermes ACP（走 _stream_hermes_acp）

```
agent.py _execute_task
  ├── env["HOME"] = sandbox_home_for("hermes", task_id)
  ├── prepare_task_env(task_id, env)                      ← hermes 设 HERMES_HOME
  │   └── build_overlay_home(iso_home, ~/.hermes)
  │       env["HERMES_HOME"] = iso_home
  │       env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
  ├── stream_runtime(env_extra=env)
  │   └── stream_agent → _stream_hermes_acp(env_extra)
  │       ├── env = acp.get_env(...)
  │       ├── env.update(env_extra)                        ← HERMES_HOME 进入 env
  │       └── acp.stream_execute(env=env)
  │           ├── iso_home = env["HERMES_HOME"]            ← fail loud
  │           ├── sandbox = sandbox_manager.prepare(...)
  │           │   bwrap: __enter__ → makedirs(env["HOME"])
  │           │   null:  __enter__ → makedirs(env["HOME"])
  │           ├── sandbox.wrap(cmd, proc_env=proc_env)
  │           └── hermes 执行
```

## 七、验证矩阵

| 引擎 | 协议 | 后端 | 期望 |
|------|------|------|------|
| hermes | acp | bwrap | completed, HERMES_HOME 指向 per-task, overlay 一次 |
| hermes | acp | none | completed, HOME 指向 per-task（不是 ~/.hermes）, overlay 一次 |
| hermes | acp | none（两个并发） | 各自 HOME 独立，无互相覆盖 |
| codearts | stream | bwrap | completed, models-cache.json 可见（层 4 修复 4802b775） ✅ 已验证 |
| codearts | stream | none | completed, GRAPE_TASK_RW_DIRS 被忽略（none 不 bind） |
| opencode | stream | bwrap | completed |
| 三引擎并发 | any | bwrap | 无 mkdir 冲突 |
| bwrap 不可用 | any | bwrap + fallback=1 | WARNING + none 成功 |
| bwrap 不可用 | any | bwrap + fallback=0 | 抛 SandboxUnavailable |
| 缺 HERMES_HOME | acp | any | 抛 RuntimeError（不静默退回默认 HOME） |

## 八、补充检查点（实现时注意）

1. bwrap `--setenv` 白名单删掉 `GRAPE_SANDBOX_HOME`（保留 `HOME`）
2. NullSandbox.wrap() 不读 GRAPE_TASK_RW_DIRS（恒等返回 cmd，是 no-op）
3. env["HOME"] 注入在 prepare_task_env 之前（先建立 per-task 基线，再引擎特殊准备）

## 九、设计决策

| 决策 | 理由 |
|------|------|
| sandbox_home_for 作为公共函数 + __all__ 导出 | 纯函数，agent.py / prepare_task_env / bwrap._build_env 共用 |
| build_overlay_home 移到 sandbox/overlay.py | 避免 runtime → agent.hermes_acp → bwrap 依赖链 |
| HOME 注入在 agent.py（prepare_task_env 之前） | 不绑 bwrap，null 降级也生效 |
| prepare_task_env 调 sandbox_home_for() 算路径 | 纯函数，不依赖 env dict 时序 |
| hermes stream_execute 从 env 读 HERMES_HOME | env 传递链确认通：prepare_task_env → env_extra → env.update → stream_execute |
| 删掉 hermes_acp fallback | fail loud，不静默退回非隔离 |
| 删掉 GRAPE_SANDBOX_HOME | 无消费方 + 与 null 语义冲突 |
| bwrap.py 删掉 os.environ fallback for GRAPE_TASK_RW_DIRS | 防止全局污染 |
| codearts GRAPE_TASK_RW_DIRS 只写 env dict | 防止 os.environ 全局污染 |
| opencode/codex 无需改动 | 无 per-task 需求，codearts 污染修复后不受影响 |

## 十、外部审查 5 个实现注意点确认（2026-10-08）

### 确认 1：engine_name / tid 来源（改动 5）

agent.py `_execute_task` 签名：`_execute_task(client, task, engine_name="")`
- `engine_name` 是引擎实例名（如 `opencode-worker-123249`）
- `tid = task.get("id", "")` 是任务 ID
- `backend_for_engine(engine_name)` 把实例名转成 backend 名（如 `opencode`、`hermes`、`codearts`）

**结论**：`sandbox_home_for` 的第一个参数用 `backend_for_engine(engine_name)`，不是 `engine_name`。

```python
# agent.py _execute_task（prepare_task_env 之前）
from src.agent.sandbox.backends.bwrap import sandbox_home_for
_backend_name = backend_for_engine(engine_name)
env["HOME"] = sandbox_home_for(_backend_name, tid)
```

### 确认 2：HOME 的 makedirs 职责唯一

**结论**：agent.py 只注入 `env["HOME"] = sandbox_home_for(...)`，**不 makedirs**。
- BwrapSandbox.__enter__：无条件 makedirs `env["HOME"]`（供 --bind source）
- NullSandbox.__enter__：无条件 makedirs `env["HOME"]`（供真实路径）
- build_bwrap_argv L438-440 已有 `os.makedirs(_home, exist_ok=True)`，保持不变

### 确认 3：_cleanup_overlay 迁移

**结论**：改动 1 补上 `_cleanup_overlay`，一并迁移到 overlay.py。
当前 `_cleanup_overlay` 只做 `shutil.rmtree(iso_home, ignore_errors=True)`，与 hermes_acp.py L829-836 的 `_res.track_overlay` + `keep_overlays=True` 逻辑冲突——实际已不再调用 `_cleanup_overlay`（TaskResourceStack 接管清理），所以只迁移定义、删除旧定义，不新增调用点。

### 确认 4：hermes_acp proc_env 的 HOME 覆写（潜在 bug）

**发现**：hermes_acp.py L622-628：
```python
proc_env = dict(env) if env else os.environ.copy()  # env 含 agent.py 注入的 per-task HOME
proc_env["HOME"] = _pwd.getpwuid(os.getuid()).pw_dir  # ← 覆写成真实 HOME！
```

**影响**：v4 设计让 bwrap._build_env 只读 `env["HOME"]`（不自己算），但 hermes_acp L626 把 proc_env["HOME"] 覆写成真实 HOME。bwrap._build_env 读到的会是真实 HOME 而非 per-task HOME。

**修正**：删除 hermes_acp.py L624-628 的 HOME 覆写。per-task HOME 由 agent.py 注入 env，经 `_stream_hermes_acp` 的 `env.update(env_extra)` 传入 stream_execute，再经 `proc_env = dict(env)` 继承。proc_env 中不覆写 HOME。

同样，executor.py L639 `_proc_env["HOME"] = _get_real_home()` 也必须删除——这条覆盖了 agent.py 注入的 per-task HOME。

### 确认 5：opencode HOME 行为变化

**发现**：v4 让 agent.py 无条件注入 `env["HOME"] = sandbox_home_for("opencode", tid)`。之前 executor.py L639 把 HOME 设成真实 HOME，bwrap._build_env L694 又自己算 per-task HOME。

**实际行为不变**：bwrap._build_env L694 之前就是 `_sandbox_home(engine_type, task_id)` = `~/.cache/grape-sandbox/opencode/<tid>/`，和 v4 的 `sandbox_home_for("opencode", tid)` 是同一个路径。只是之前 bwrap 自己算，现在改为从 `env["HOME"]` 读。

**opencode 配置可见性**：opencode 的配置文件（如 `~/.config/opencode`）通过 bwrap --ro-bind 挂载宿主机路径，不走 HOME 前缀映射（bwrap.py L325-330 明确注释「不做 HOME 前缀映射，直接 bind 到宿主机路径」）。所以 per-task HOME 不影响 opencode 找到配置。

**结论**：opencode 行为不变，无需额外改动。验证矩阵补充一行。

### 验证矩阵补充

| 引擎 | 协议 | 后端 | 期望 |
|------|------|------|------|
| opencode | stream | bwrap | completed, 能读到原有配置（--ro-bind 宿主机路径，不依赖 HOME 前缀） |

## 十一、深度检测 + 硬编码扫描（2026-10-08）

### 深度检测：env 传递链完整性

逐行追踪 HOME 从 agent.py 到 bwrap --bind 的完整路径：

路径 A（opencode/codearts/codex，走 _run_once）：
```
agent.py L728: env["HOME"] = sandbox_home_for(backend_for_engine(engine_name), tid)
agent.py L733: _backend.prepare_task_env(tid, env, _res)  ← codearts 从 env["HOME"] 读
→ stream_runtime(env_extra=env) → stream_agent → _run_once
→ _proc_env = env（含 HOME + GRAPE_TASK_RW_DIRS）
→ executor.py L639: (已删除 _proc_env["HOME"] = _get_real_home())
→ _sandbox.wrap(cmd, proc_env=_proc_env)
  → bwrap._build_env: env["HOME"] = proc_env["HOME"]  ← per-task HOME ✓
  → build_bwrap_argv L315: safe_env.get("HOME") = per-task HOME ✓
  → build_bwrap_argv L438: sandbox_home_for(engine_type, task_id)  ← 与 L315 一致 ✓
```

路径 B（hermes ACP，走 _stream_hermes_acp）：
```
agent.py L728: env["HOME"] = sandbox_home_for("hermes", tid)
agent.py L733: hermes.py prepare_task_env → build_overlay_home + env["HERMES_HOME"] = iso_home
→ stream_agent → _stream_hermes_acp(env_extra=env) → env.update(env_extra)
→ hermes_acp.stream_execute(env=env)
  → L601: iso_home = env.get("HERMES_HOME", "")  ← fail loud ✓
  → L607: proc_env = dict(env)  ← 继承 HOME + HERMES_HOME
  → L609-610: proc_env["HERMES_HOME"] / HERMES_YOLO_MODE
  → _sandbox.wrap(cmd, proc_env=proc_env)
    → bwrap._build_env: env["HOME"] = proc_env["HOME"]  ← per-task HOME ✓
    → HERMES_HOME / HERMES_DISABLE_LAZY_INSTALLS 从 proc_env 透传到 safe_env ✓
```

engine_type 一致性确认：
- agent.py: `backend_for_engine(engine_name)` → 如 "opencode"
- executor.py L623: `sandbox_manager.prepare(exec_backend=backend.name, ...)`
- manager.py L208-211: `ctx.engine_type` or `backend_for_engine(exec_backend)` fallback
- `exec_backend = backend.name = backend_for_engine(engine_name)` → 一致 ✓

### 深度检测：HERMES_HOME 透传到 bwrap --setenv

bwrap._build_env 删除 `if self._engine_type == "hermes"` 块后：
1. `HERMES_HOME` 在 hermes.py prepare_task_env 中设到 env dict
2. env → hermes_acp proc_env → bwrap._build_env proc_env
3. _build_env L718-720: proc_env 遍历，HERMES_HOME 不在黑名单 → 透传到 safe_env
4. build_bwrap_argv L226-227: `--setenv HERMES_HOME <iso_home>` ✓
5. HERMES_HOME 不以 _TOKEN/_SECRET/_KEY/_PASSWORD/_CREDENTIALS 结尾 → 不被 secret suffix deny ✓

### 硬编码扫描

扫描全部改动文件，发现 1 处 v5 引入的硬编码：

| 文件 | 行 | 问题 | 修复 |
|------|----|------|------|
| hermes.py L412 | `sandbox_home_for("hermes", task_id)` | 硬编码引擎名 | 改为 `sandbox_home_for(self.name, task_id)` |

其他硬编码（引擎类属性 name/binary、_BACKENDS 注册表、/tmp workdir 等）均为原有代码，非 v5 引入。

### 死代码清理

- executor.py: 删除 `_get_real_home()` 函数定义（无调用点）+ 移除 `import pwd`
- hermes_acp.py: 删除 `_build_overlay_home()` 和 `_cleanup_overlay()` 定义（已迁移到 overlay.py）
