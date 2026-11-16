# bwrap 沙箱 HOME 路径重构设计方案（v3 最终版）

## 问题

当前沙箱 HOME = `/tmp/grape-home`，存在三个根本问题：

1. **会被清理**：systemd-tmpfiles 定期清 `/tmp`，重启清空。引擎 config/session/依赖状态丢失。
2. **可能 noexec**：部分发行版 `/tmp` 挂 noexec，可执行文件不能执行。
3. **路径不一致**：HOME 前缀映射把 `/root/.local/bin/hermes` → `/tmp/grape-home/.local/bin/hermes`，但 hermes 通过 PATH 找到 `/root/.local/bin/hermes`（宿主机路径），沙箱里不存在 → `No such file or directory`。

## 目标

- 沙箱 HOME 持久、可执行、不会被清理
- binary/config/数据路径在沙箱内外一致
- 引擎间隔离、任务间隔离
- 不破坏 opencode/codearts 现有逻辑
- HOME 跟着登录用户走，不硬编码

## 方案

### 核心思路

沙箱 HOME = `~/.cache/grape-sandbox/<engine_type>/<task_id>/`，通过 `os.path.expanduser("~")` 获取当前登录用户的 HOME。

HOME 前缀映射**不删**，只改目标：从 `/tmp/grape-home` 改为新的持久 HOME 路径。

hermes 的硬编码路径（`~/.local/bin/hermes`、`~/.hermes`）用显式直 bind 到宿主机路径，不走 HOME 前缀映射。

### /root 下的 bind 覆盖顺序

不 `--ro-bind /root`（避免暴露敏感文件）。用 `--dir` 创建空目录，然后只 bind 需要的子路径：

```
1. --dir ~                              ← 创建空 HOME 根目录
2. 引擎 RO 路径直 bind（不走 HOME 映射）
   --ro-bind ~/.hermes ~/.hermes         ← 目录
   --dir ~/.local
   --dir ~/.local/bin
   --ro-bind ~/.local/bin/hermes ~/.local/bin/hermes  ← 单文件
   --ro-bind /usr/local/lib/hermes-agent ...
3. --bind $SANDBOX_HOME $SANDBOX_HOME   ← per-task HOME（RW）
4. HOME 前缀映射的 RO/RW（保留现有逻辑，目标改为 $SANDBOX_HOME）
   --ro-bind ~/.config/opencode $SANDBOX_HOME/.config/opencode
5. --tmpfs /tmp                         ← 临时文件隔离（不再当 HOME）
6. UDS、cwd 等
```

### 沙箱 HOME 路径计算

```python
_SANDBOX_HOME_BASE = os.path.join(os.path.expanduser("~"), ".cache", "grape-sandbox")

def _sandbox_home(engine_type: str = "", task_id: str = "") -> str:
    """计算沙箱 HOME 路径：~/.cache/grape-sandbox/<engine_type>/<task_id>/"""
    parts = [_SANDBOX_HOME_BASE]
    if engine_type:
        parts.append(engine_type)
    if task_id:
        parts.append(task_id)
    return os.path.join(*parts)
```

### 改动点

#### 1. bwrap.py

**`_SANDBOX_HOME` 常量改为函数：**
- `_sandbox_home(engine_type, task_id)` 返回 per-task 路径
- 旧代码引用 `_SANDBOX_HOME` 的地方改为调用函数

**`build_bwrap_argv`：**
- 新增 `engine_type` 和 `task_id` 参数
- `_sandbox_home` 从 `safe_env["HOME"]` 读（`_build_env` 设的 per-task 路径）
- 单文件 RO bind 不做 HOME 前缀映射，直接 bind 到宿主机路径 + 逐层 `--dir` 父目录
- 目录 RO bind 保留 HOME 前缀映射（目标改为新的 `_sandbox_home`）
- `--tmpfs /tmp` 保留，不再当 HOME
- 沙箱 HOME 用 `--bind`（RW）挂载 per-task 目录
- backend 查询用 `engine_type`，不用 `exec_backend`（实例名）
- API 查询 `sandbox-paths` 的 `backend_name` 用 `engine_type`

**`_build_env`：**
- `HOME` = per-task 沙箱目录（统一所有引擎，不再分 hermes overlay / 其他）
- 删除 `bwrap_overlay_fn` 特殊分支
- `GRAPE_SANDBOX_HOME` = `HOME`
- backend 查询用 `self._engine_type`

**`BwrapSandbox`：**
- `__init__` 接收 `engine_type` 和 `task_id` 参数
- 存 `self._engine_type` 和 `self._task_id`
- `wrap` 方法调 `build_bwrap_argv` 时传 `engine_type=self._engine_type`
- cleanup 时删除 per-task HOME 目录

**`BwrapProvider.create_sandbox`：**
- 从 kwargs 取 `engine_type` 和 `task_id`
- 用 `engine_type` 查 backend
- 传给 `BwrapSandbox`

#### 2. hermes.py

- 删除 `bwrap_overlay_fn` 属性
- `hermes_overlay_path` 函数标记废弃
- hermes 的持久化 HOME 由统一的 per-task 沙箱 HOME 处理

#### 3. agent.py

- `GRAPE_SANDBOX_HOME` 日志改为打印实际 per-task 路径（从 `runtime.env` 读，不再硬编码 `_SANDBOX_HOME`）

#### 4. engine_type 传递链

```
manager.py prepare()
  → _engine_type = ctx.engine_type or backend_for_engine(exec_backend)
  → create_sandbox(engine_type=_engine_type, task_id=ctx.task_id)
    → BwrapSandbox(engine_type=_engine_type, task_id=ctx.task_id)
      → _build_env(): HOME = _sandbox_home(engine_type, task_id)
      → build_bwrap_argv(engine_type=self._engine_type)
        → backend = get_backend_or_none(engine_type)
        → _sandbox_home = safe_env["HOME"]
```

### 清理语义

| 场景 | 清理时机 | 谁清 | 清不掉怎么办 |
|------|---------|------|-------------|
| 任务正常完成 | `BwrapSandbox.cleanup()` | SandboxContext try/finally | 日志 warning |
| 任务失败 | 同上 | 同上 | 同上 |
| 任务超时 | executor kill 进程后 cleanup | executor try/finally | 同上 |
| Worker 被 kill | 下次 worker 启动 | sweeper 扫描 | 目录残留，下次清理 |
| Worker 崩溃 | 同上 | 同上 | 同上 |

**sweeper 逻辑**（worker 启动时执行）：
1. 扫描 `~/.cache/grape-sandbox/<engine>/*/` 所有子目录
2. 查 DB 里 status 为 dispatched/running 的 task_id
3. 不在活跃列表里的目录，删除
4. 日志记录清理了哪些目录

### 不改的部分

- `--tmpfs /tmp` 保留
- `--unshare-net` 保留
- `--ro-bind /usr /lib /bin` 等系统路径不变
- opencode/codearts 的 `sandbox_ro_paths` / `sandbox_rw_paths` 不变
- codearts 的 `KERNEL_DATA_DIR` per-task 隔离不变
- `GRAPE_TASK_RW_DIRS` 机制不变
- HOME 前缀映射逻辑不变（只改目标路径）

### 最终 bind 逻辑

```
系统 RO:      --ro-bind /usr /usr  --ro-bind /lib /lib ...
              --dir ~                                ← 创建空 HOME 根
引擎 RO:      --ro-bind ~/.hermes ~/.hermes          ← 目录, 直 bind
              --dir ~/.local --dir ~/.local/bin
              --ro-bind ~/.local/bin/hermes ~/.local/bin/hermes  ← 单文件
              --ro-bind /usr/local/lib/hermes-agent ...
沙箱 HOME:    --bind $SANDBOX_HOME $SANDBOX_HOME     ← RW, per-task
HOME 映射:    --ro-bind ~/.config/opencode $SANDBOX_HOME/.config/opencode
tmpfs:        --tmpfs /tmp
UDS:          --bind ~/.grape-sandbox-uds ~/.grape-sandbox-uds
cwd:          --bind $cwd $cwd --chdir $cwd
```

### 验证清单

1. hermes bwrap：binary 找到、ACP 初始化成功、LLM 回复输出
2. opencode bwrap：config 读取正常、LLM 回复输出
3. codearts bwrap：cli-data 写入正常、LLM 回复输出
4. 并发：两个同引擎任务同时跑，HOME 隔离
5. 清理：任务结束后目录被删除
6. sweeper：worker 重启时清理残留目录
