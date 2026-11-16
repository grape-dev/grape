# bwrap 沙箱抽象边界重构设计（v6）

> 日期：2026-10-08
> 前置文档：docs/weekly-05/bwrap-per-task-env-decouple-design.md（v5）
> tag: bwrap-v5-codearts-fixed（v5 修复完成节点）

## 一、问题

v5 重构后，agent.py 无条件注入 per-task HOME 和调用 prepare_task_env，
导致非 bwrap 模式（NullSandbox）偏离基线 weekly-05 行为。

### 问题 1：codearts 非 bwrap text_len=0

codearts installer 脚本无条件 `export KERNEL_DATA_DIR="$HOME/.codeartsdoer/cli-data"`。
v5 agent.py 无条件注入 `env["HOME"] = sandbox_home_for(...)` → per-task 路径。
非 bwrap 模式下没有 bwrap --setenv 覆盖 installer 的 export，
installer 用 per-task HOME 拼接 → 指向空目录（缺 opencode.db）→ database migration → stdout 无输出。

基线 weekly-05 不改 HOME（HOME=/root），installer export 指向宿主机 cli-data（有 opencode.db），不触发 migration。

### 问题 2：hermes 非 bwrap HERMES_HOME missing

v4 把 HERMES_HOME overlay 构建从 stream_execute 内部移到 prepare_task_env，
stream_execute 改为 fail loud 依赖外部注入。但非 bwrap 模式 NullSandbox.prepare_env 是空操作，
不调 prepare_task_env → env 里没有 HERMES_HOME → fail loud。

基线 weekly-05 的 stream_execute 自己构建 overlay（不依赖外部），不管 bwrap 与否。

### 问题 3：NullSandbox wrap 恢复 HOME 导致耦合

v5 NullSandbox.wrap 里恢复 HOME 为宿主机路径，但 prepare_task_env 已经用 per-task HOME
算出 GRAPE_TASK_RW_DIRS target 路径 → target 和实际 HOME 不一致 → bwrap/非 bwrap 耦合。

## 二、设计原则

**bwrap 和非 bwrap 是同一个抽象（Sandbox）的两个实现。非 bwrap 的实现行为应和基线 weekly-05 完全一致。**

核心原则：
- **面向接口编程**：agent.py 和 executor.py 只面向 Sandbox 接口，不感知具体实现
- **高内聚低耦合**：per-task HOME 注入和 prepare_task_env 是 bwrap 隔离的内部实现细节，不应泄漏到 agent.py
- **策略模式**：BwrapSandbox 和 NullSandbox 是同一接口的两种策略，各自封装自己的行为
- **NullSandbox = 基线行为**：不注入 per-task HOME、不调 prepare_task_env、直接返回 cmd

## 三、耦合点审查（7 个）

| # | 位置 | bwrap | 非 bwrap | 基线 | 改造 |
|---|------|-------|---------|------|------|
| 1 | agent.py L731: HOME 注入 | per-task HOME + bwrap --bind/--setenv | per-task HOME 但无 bind/setenv → installer 拼空目录 | HOME=/root | **移到 BwrapSandbox.prepare_env** |
| 2 | agent.py L739: prepare_task_env | 创建 per-task 目录 + GRAPE_TASK_RW_DIRS | 创建了但 installer 覆盖 KERNEL_DATA_DIR → 无用 | 基线也有但同样被覆盖 | **移到 BwrapSandbox.prepare_env** |
| 3 | null.py: HOME 恢复 | 不执行 | 恢复 HOME=宿主机，但 prepare_task_env 已用 per-task HOME 算路径 → 不一致 | 基线无 NullSandbox | **删除（不再需要）** |
| 4 | executor.py L633: GRAPE_SANDBOX_MODE | 设为 'bwrap' | 设为 'none' | 基线无此变量 | 不改（正确抽象） |
| 5 | bwrap.py L700: _build_env 读 HOME | 从 proc_env 读 per-task HOME | 不执行 | 基线无 bwrap | 不改（bwrap 内部） |
| 6 | bwrap.py L403: HOME --bind | bind per-task HOME | 不执行 | 基线无 bwrap | 不改（bwrap 内部） |
| 7 | hermes_acp.py: HERMES_HOME fail loud | bwrap 模式 prepare_env 注入 | 非 bwrap 模式 env 里没有 → fail loud | 基线自己构建 overlay | **stream_execute 加 fallback 自建 overlay** |

## 四、方案选型

### 方案 A：在 wrap() 里做（❌ 不选）

问题：wrap() 语义是"包装命令"，可多次调用，prepare_task_env 只应执行一次。

### 方案 B：在 __enter__ 里做（❌ 不选）

问题：__enter__ 不接收参数，无法拿到 env dict 和 engine_backend。

### 方案 C：Sandbox 接口加 prepare_env() 方法（✅ 选这个）

Sandbox 接口新增 `prepare_env(env, engine_backend, resource_stack)` 方法：
- BwrapSandbox.prepare_env：注入 per-task HOME + 调 engine_backend.prepare_task_env
- NullSandbox.prepare_env：空操作（基线行为）

优点：面向接口、高内聚低耦合、单一职责、NullSandbox = 基线。

### 方案 D：BwrapProvider.create_sandbox 传入 engine_backend + env（❌ 不选）

问题：Provider 应无状态，env 引用可能过期。

## 五、最终方案（方案 C）

### 改造 1：LocalSandbox 接口加 prepare_env

```python
# src/agent/sandbox/api.py
class LocalSandbox(Sandbox, Protocol):
    def prepare_env(self, env: dict[str, str], engine_backend: Any, resource_stack: Any = None) -> None:
        """准备引擎 per-task 环境（在 __enter__ 之后、wrap 之前调用一次）。

        BwrapSandbox：注入 per-task HOME + 调 engine_backend.prepare_task_env。
        NullSandbox：空操作（env 不被修改，HOME 保持宿主机路径）。
        """
        ...
```

### 改造 2：BwrapSandbox 实现 prepare_env

```python
# src/agent/sandbox/backends/bwrap.py
class BwrapSandbox:
    def prepare_env(self, env, engine_backend, resource_stack=None) -> None:
        """bwrap 隔离：注入 per-task HOME + 调 engine prepare_task_env。"""
        _home = sandbox_home_for(self._engine_type, self._task_id)
        os.makedirs(_home, exist_ok=True)
        env["HOME"] = _home
        if engine_backend and hasattr(engine_backend, "prepare_task_env"):
            engine_backend.prepare_task_env(self._task_id, env, resource_stack)
```

### 改造 3：NullSandbox 实现 prepare_env（空操作）

```python
# src/agent/sandbox/backends/null.py
class NullSandbox:
    def prepare_env(self, env, engine_backend, resource_stack=None) -> None:
        """NullSandbox = 不隔离：不注入 per-task HOME，不调 prepare_task_env。"""
        pass  # 基线行为——env 不被修改，HOME 保持宿主机路径
```

### 改造 4：agent.py 删除 HOME 注入 + prepare_task_env 调用

agent.py 不再感知沙箱模式，不调 sandbox_home_for，不调 prepare_task_env。
验证：`grep -c "sandbox_home_for\|prepare_task_env" src/runtime/agent.py` = 0

### 改造 5：executor.py 在 __enter__ 之后调 prepare_env

```python
# src/agent/executor.py _run_once
_sandbox.__enter__()
_prepare = getattr(_sandbox, "prepare_env", None)
if _prepare:
    _prepare(_proc_env, backend)
_cmd = _sandbox.wrap(_cmd, proc_env=_proc_env)
```

### 改造 6：hermes_acp.py stream_execute 加 fallback 自建 overlay

```python
# src/agent/hermes_acp.py stream_execute
iso_home = (env or {}).get("HERMES_HOME", "")
if not iso_home:
    # 非 bwrap 模式 fallback——自己算 iso_home + 构建 overlay（和基线 weekly-05 一致）
    _cwd_hash = hashlib.md5((cwd or ".").encode()).hexdigest()[:12]
    iso_home = os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes", _cwd_hash)
    os.makedirs(iso_home, exist_ok=True)
    build_overlay_home(iso_home, os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
```

### 改造 7：hermes_acp.py stream_execute 里也调 sandbox.prepare_env

```python
# src/agent/hermes_acp.py stream_execute
_sandbox.__enter__()
_hermes_runtime = get_backend_or_none("hermes")
_prepare = getattr(_sandbox, "prepare_env", None)
if _prepare:
    _prepare(proc_env, _hermes_runtime)
_acp_cmd = _sandbox.wrap(_acp_cmd, proc_env=proc_env)
```

## 六、传递链确认

### bwrap 路径
```
agent.py _execute_task
  ├── env = backend.get_env()           ← 不注入 HOME
  ├── stream_runtime(env_extra=env)
  │   └── stream_agent → _run_once
  │       ├── _proc_env = env
  │       ├── sandbox = sandbox_manager.prepare(...)
  │       ├── sandbox.__enter__()                    ← 启动 network/limiter
  │       ├── sandbox.prepare_env(_proc_env, backend)
  │       │   bwrap: HOME = sandbox_home_for(...) → env["HOME"] = HOME
  │       │          backend.prepare_task_env(task_id, env, _res)
  │       │          → env["KERNEL_DATA_DIR"] = per-task
  │       │          → env["GRAPE_TASK_RW_DIRS"] = [...]
  │       ├── _cmd = sandbox.wrap(_cmd, proc_env=_proc_env)
  │       │   bwrap: _build_env 读 env["HOME"] → safe_env["HOME"] → --setenv HOME <per-task>
  │       │          build_bwrap_argv: --setenv KERNEL_DATA_DIR <per-task>（覆盖 installer export）
  │       └── Popen(_cmd, env=_proc_env)
```

### 非 bwrap 路径
```
agent.py _execute_task
  ├── env = backend.get_env()           ← 不注入 HOME
  ├── stream_runtime(env_extra=env)
  │   └── stream_agent → _run_once
  │       ├── _proc_env = env
  │       ├── sandbox = sandbox_manager.prepare(...)
  │       ├── sandbox.__enter__()                    ← 空操作
  │       ├── sandbox.prepare_env(_proc_env, backend)
  │       │   null: pass（什么都不做）
  │       ├── _cmd = sandbox.wrap(_cmd, proc_env=_proc_env)
  │       │   null: 清除 proxy env，返回原 cmd
  │       └── Popen(_cmd, env=_proc_env)
  │           HOME=/root（宿主机），installer export KERNEL_DATA_DIR=/root/.codeartsdoer/cli-data
  │           ← 和基线 weekly-05 完全一致
```

### hermes ACP 非 bwrap 路径
```
agent.py _execute_task
  ├── env = backend.get_env()           ← 不注入 HOME
  ├── stream_agent → _stream_hermes_acp
  │   └── HermesACPBackend.stream_execute(env=env)
  │       ├── iso_home = env.get("HERMES_HOME", "")  ← 空（非 bwrap 没注入）
  │       ├── fallback: 自己算 iso_home + build_overlay_home（和基线一致）
  │       ├── proc_env["HERMES_HOME"] = iso_home
  │       ├── sandbox.__enter__()
  │       ├── sandbox.prepare_env(proc_env, hermes_runtime)  ← null: pass
  │       ├── sandbox.wrap(_acp_cmd, proc_env=proc_env)     ← null: 返回原 cmd
  │       └── Popen(_acp_cmd, env=proc_env)
  │           HOME=/root（宿主机），HERMES_HOME=overlay 路径
  │           ← 和基线 weekly-05 完全一致
```

## 七、影响面分析

| 引擎 | bwrap | 非 bwrap |
|------|-------|---------|
| codearts | prepare_env 注入 HOME=per-task + prepare_task_env。bwrap --setenv 覆盖 installer export | prepare_env 空操作。HOME=/root，installer export 用宿主机路径，有 opencode.db，不 migration |
| hermes | prepare_env 注入 HOME=per-task + prepare_task_env 构建 overlay + HERMES_HOME | prepare_env 空操作。stream_execute fallback 自建 overlay。HOME=/root |
| opencode | prepare_env 注入 HOME=per-task（opencode 无 prepare_task_env） | prepare_env 空操作。HOME=/root |

## 八、改造文件清单

| 文件 | 改动 |
|------|------|
| src/agent/sandbox/api.py | LocalSandbox 接口加 prepare_env 方法 |
| src/agent/sandbox/backends/bwrap.py | BwrapSandbox 实现 prepare_env |
| src/agent/sandbox/backends/null.py | NullSandbox 实现 prepare_env（空操作）+ 删除 wrap 里的 HOME 恢复 |
| src/runtime/agent.py | 删除 HOME 注入 + prepare_task_env 调用 |
| src/agent/executor.py | __enter__ 后调 sandbox.prepare_env |
| src/agent/hermes_acp.py | stream_execute 加 fallback 自建 overlay + 调 sandbox.prepare_env |

## 九、验证标准

| 检查项 | 标准 |
|--------|------|
| codearts bwrap | text_len > 0 |
| codearts 非 bwrap | text_len > 0（installer export 用宿主机路径，不 migration） |
| hermes bwrap | text_len > 0 |
| hermes 非 bwrap | text_len > 0 |
| opencode bwrap | text_len > 0 |
| opencode 非 bwrap | text_len > 0 |
| agent.py 不感知沙箱 | grep -c "sandbox_home_for\|prepare_task_env" agent.py = 0 |

## 十、验证结果（194 机器，2026-10-08）

### bwrap 模式（OBS_ENABLED=true）

| 引擎 | issue | text_len | 状态 |
|------|-------|---------|------|
| codearts | #51 | 35 | ✅ completed |
| hermes | #55 | 203/331 | ✅ completed |
| opencode | #56 | 66 | ✅ completed |

### 非 bwrap 模式（OBS_ENABLED=true）

| 引擎 | issue | VOD text_len | issue 阶段 text_len | 状态 |
|------|-------|-------------|-------------------|------|
| codearts | #73 | 382 | - | ✅ completed |
| codearts | #74 | 431 | - | ✅ completed |
| opencode | #72 | 58/378 | running | ✅ |
| opencode | #73 | 58 | - | ✅ completed |
| opencode | #74 | 69 | - | ✅ completed |
| hermes | #74 | - | 569 | ✅ running |

## 十一、排查过程中的错误假设（记录以防重新分析）

1. **"Bun stdout 缓冲问题"**：BUN_STDOUT_BUFFERING=none 正确透传，不是缓冲问题。
2. **"PTY 包裹方案"**：script -q -c 能让非 bwrap 输出 NDJSON，但 bwrap 不需要 PTY，治标不治本。
3. **"非 bwrap 超时 = bwrap 问题"**：非 bwrap 模式 codearts 连上外网走 xCenter 路径，stdout 行为不同，与 bwrap 问题无关。
4. **"worker 路径和手动测试代码不同"**：worker 和手动测试用相同代码路径，差异在 env 内容，但 env 差异不影响 stdout。
5. **"NullSandbox wrap 恢复 HOME"**：恢复 HOME 导致 prepare_task_env 算的 GRAPE_TASK_RW_DIRS target 路径和实际 HOME 不一致，耦合更严重。
6. **"agent.py 用 GRAPE_AGENT_SANDBOX 环境变量判断 bwrap"**：PREFERRED 模式下 Provider 不可用会降级到 NullSandbox，此时 GRAPE_AGENT_SANDBOX 可能还是 on，判断不准确。
7. **"hermes ACP stream_execute fail loud 依赖外部 prepare_task_env"**：非 bwrap 模式 NullSandbox.prepare_env 是空操作，不调 prepare_task_env → env 里没有 HERMES_HOME → fail loud。基线 stream_execute 自己构建 overlay，不依赖外部。

## 十二、commit 列表

| commit | 改动 |
|--------|------|
| f26c6366 | v6 沙箱抽象边界——prepare_env 接口方法解耦 bwrap/非 bwrap |
| 505f2a4b | hermes ACP 路径也调 sandbox.prepare_env |
| ffae301c | hermes ACP 非 bwrap 模式自己构建 HERMES_HOME overlay（和基线一致） |
