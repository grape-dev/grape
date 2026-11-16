# codearts bwrap 沙箱 text_len=0 根因修复设计

## 日期
2026-10-08

## 问题
codearts 在 bwrap 沙箱模式下 exit=0 但 text_len=0，无 LLM 回复输出。

## 根因全景（4 层）

### 层 1：`rw_paths` 声明 `~/.codeartsdoer` 与 `GRAPE_TASK_RW_DIRS` 冲突

codearts.py `rw_paths` 声明了 `~/.codeartsdoer`。

bwrap.py 公共 bind 逻辑：
1. L374-379：`~/.codeartsdoer` 展开为 `/root/.codeartsdoer`，`startswith("/root/")` = True → bind source=target=`/root/.codeartsdoer`（宿主机路径自身 bind）
2. L388-390：`_handled_targets` 加入 `_sandbox_home_path + "/.codeartsdoer"`（沙箱 HOME 下的路径）

codearts.py `prepare_task_env` 构造 `GRAPE_TASK_RW_DIRS`：
```json
[
  {"source": "/root/.codeartsdoer", "target": "<sandbox_home>/.codeartsdoer", "mode": "ro"},
  {"source": "<per-task cli-data>", "target": "<sandbox_home>/.codeartsdoer/cli-data", "mode": "rw"}
]
```

bwrap.py L391-393：第一个 target 在 `_handled_targets` 里 → **跳过 RO bind**。

**修复**：从 `rw_paths` 删掉 `~/.codeartsdoer`。commit d9ac59a9。

### 层 2：`GRAPE_TASK_RW_DIRS` 被 `_build_env` 吞掉

即使删掉 `rw_paths` 里的 `~/.codeartsdoer`，`GRAPE_TASK_RW_DIRS` 仍然不生效。

bwrap.py env 传递链：
1. `_build_env(proc_env)` L718-720：`GRAPE_TASK_RW_DIRS` 不在 `_BLOCKED_VARS` 里 → 复制到 `safe_env`
2. `build_bwrap_argv` L222-225：`safe_env` 里的 key 从 `proc_env` 里删除（`proc_env.pop(k, None)`）
3. `build_bwrap_argv` L362：`(proc_env or {}).get("GRAPE_TASK_RW_DIRS", "")` → 读到空字符串
4. per-task bind 不执行

**根因**：`GRAPE_TASK_RW_DIRS` 是 bwrap bind 配置，不是引擎环境变量。它不应该被 `_build_env` 复制到 `safe_env`（不需要透传到沙箱内 `--setenv`），应该留在 `proc_env` 里供 `build_bwrap_argv` 读取。

**修复**：将 `GRAPE_TASK_RW_DIRS` 加入 `_BLOCKED_VARS`。commit ec456cb3。

### 层 3：沙箱 HOME `--bind` 顺序错误

层 2 修复后 `GRAPE_TASK_RW_DIRS` 能传到 `build_bwrap_argv`，但 per-task bind 仍然失败：
`GRAPE_TASK_RW_DIRS` 的 target 在沙箱 HOME 路径下，需要先 `--bind HOME` 使其可写，否则 `--dir` 创建子目录时 HOME 父路径是 RO（`/root/.cache` 被 `--ro-bind`）。

**修复**：沙箱 HOME `--bind` 移到 `_post_tmpfs_binds` 之前。commit 7f92f41e。

### 层 3b：`_handled_targets` 误跳 per-task bind

`_handled_targets` 用沙箱 HOME 前缀映射后的 target 判断是否已处理，但 HEAD 时代 HOME 前缀不做映射，rw_paths 中 HOME 前缀路径 bind target 是宿主机路径自身。`_handled_targets` 仍用旧映射逻辑导致 per-task bind 被误跳。

**修复**：`_handled_targets` 用实际 bind target（宿主机路径）。commit 90f521db。

### 层 4（最终根因）：per-task HOME 下缺 `models-cache.json`

层 1-3 修复后 codearts 不再报 database migration，新报错 `loadXCenterModels 失败`——exit=0 但 stdout 无 NDJSON 输出。

#### 对比实验证据（194 机器）

| 测试 | HOME 值 | stdout 行数 | stderr | 结果 |
|------|---------|------------|--------|------|
| 手动测试 A | `/root` | 3 行 NDJSON | 0 行 | text="你好！我是 CodeArts..." ✅ |
| 手动测试 B | `/root/.cache/grape-sandbox/codearts/test-002` | 0 行 | 3 行（loadXCenterModels 失败） | text_len=0 ❌ |
| 手动测试 C（修复后） | 同 B | 3 行 NDJSON | 0 行 | text="你好！我是 CodeArts..." ✅ |

#### 根因分析

codearts 用 `$HOME/.cache/codeartsdoer/models-cache.json` 作为本地模型缓存。codearts 启动时：
1. 尝试连华为云 xCenter 拉模型列表 → 网络隔离（bwrap --unshare-net）→ 503 失败
2. fallback 到本地缓存 `$HOME/.cache/codeartsdoer/models-cache.json`
3. 缓存不存在 → `loadXCenterModels` 抛异常 → codearts 进程 exit=0 但 stdout 无 NDJSON 输出

**7705b9fa 时代为什么能工作**：
- HOME = `/tmp/grape-home`（tmpfs 下的固定路径）
- HOME 前缀路径做映射：`/root/.cache` → `/tmp/grape-home/.cache`（bwrap.py 旧逻辑 `_target = _sandbox_home + p[len(_real_home):]`）
- codearts 在沙箱内 `$HOME/.cache/codeartsdoer/models-cache.json` = `/tmp/grape-home/.cache/codeartsdoer/models-cache.json` → RO bind 到宿主机 `/root/.cache/codeartsdoer/models-cache.json` → 缓存可见

**HEAD 时代为什么不工作**：
- HOME = `~/.cache/grape-sandbox/codearts/<task_id>/`（per-task 持久路径）
- HOME 前缀路径不做映射，直接 bind 到宿主机路径
- codearts 在沙箱内 `$HOME/.cache/codeartsdoer/models-cache.json` = `/root/.cache/grape-sandbox/codearts/<task_id>/.cache/codeartsdoer/models-cache.json`
- per-task HOME 是空目录，`.cache/codeartsdoer/` 不存在 → `loadXCenterModels` 失败

codearts 日志确认（194 机器 `/var/cache/codearts-tasks/t_afb8f89adbaa/cli-data/log/`）：
```
WARN  service=provider-models-plugin "agent_id is empty, skipping xCenter models fetch"
INFO  service=provider-models-plugin "loaded models from cache" path=/root/.cache/codeartsdoer/models-cache.json
```
上述日志是 HOME=/root 时的情况（缓存可见）。HOME=per-task 路径时缓存不可见，走 loadXCenterModels 抛异常路径。

#### 修复方案

codearts `prepare_task_env` 的 `GRAPE_TASK_RW_DIRS` 增加一条 `~/.cache/codeartsdoer` → `per-task HOME/.cache/codeartsdoer` 的 RO bind：

```python
# src/runtime/codearts.py prepare_task_env

_sandbox_models_cache_dir = _os.path.join(_sandbox_home, ".cache", "codeartsdoer")
_host_models_cache_dir = _os.path.join(_home, ".cache", "codeartsdoer")

_task_rw_dirs = _json_mod.dumps([
    {"source": _host_source, "target": _sandbox_codeartsdoer, "mode": "ro"},
    {"source": _cli_data, "target": _sandbox_cli, "mode": "rw"},
    {"source": _host_models_cache_dir, "target": _sandbox_models_cache_dir, "mode": "ro"},  # 新增
])
```

commit 4802b775。

#### 为什么不用其他方案

| 方案 | 为什么不用 |
|------|-----------|
| 恢复 HOME 前缀映射 | hermes 内部硬编码 `/root/.hermes/tools/...`，HOME 前缀映射会导致 hermes 找不到路径 |
| 把 `/root/.cache` 整体 RO bind 到 per-task HOME | `/root/.cache` 包含 grape-sandbox 自身和其他引擎缓存，范围过大 |
| 在 per-task HOME 下 symlink `.cache` → 宿主机 `.cache` | symlink 在 bwrap --bind 后的目录内创建，跨 bind 边界不可靠 |
| 只用 `GRAPE_TASK_RW_DIRS` 精确 bind 单个目录 | 最小权限，只暴露 `~/.cache/codeartsdoer` 一个目录 ✅ |

#### 排查过程中的错误假设（记录以防重新分析）

1. **"Bun stdout 缓冲问题"**——曾假设 `BUN_STDOUT_BUFFERING=none` 不生效导致 stdout 块缓冲。
   实际验证：`BUN_STDOUT_BUFFERING=none` 在 bwrap `--setenv` 里正确透传。手动测试 stdout=3 行证明缓冲不是问题。
   非缓冲问题的证据：HOME=/root 时 stdout 正常输出 3 行 NDJSON。

2. **"codearts stdout 缓冲需要 PTY 包裹"**——曾尝试 `script -q -c` PTY 方案。
   实际验证：PTY 方案虽能让非 bwrap 模式输出 NDJSON，但 bwrap 手动测试不需要 PTY 也能输出。
   PTY 方案是治标不治本——真正问题是 models-cache.json 缺失导致 codearts 走异常路径。

3. **"非 bwrap 直接跑 codearts stdout 空 = bwrap 问题"**——非 bwrap 模式 codearts 30 秒超时 stdout 空。
   实际验证：这是 codearts 在有网络时走 xCenter 路径的行为（strace 显示 connect 到华为云 443），
   与 bwrap `--unshare-net` 隔离后走 fallback 路径的行为不同。非 bwrap stdout 空不是 bwrap 的问题。

4. **"worker 路径和手动测试代码路径不同"**——曾对比 env 差异、prompt 差异、bwrap argv 差异。
   实际验证：env 差异仅 3 个无关变量（VCS_API_BASE/VCS_NAME/DEVOPS_EVENT_TYPE），prompt 短/长都通过。
   最终发现 worker 路径真实 issue #51 text_len=35 验证通过——之前的 text_len=0 是修复前跑的旧任务。

## 修复 commit 列表

| 层 | commit | 修复内容 |
|----|--------|---------|
| 1 | d9ac59a9 | rw_paths 删 `~/.codeartsdoer` |
| 2 | ec456cb3 | GRAPE_TASK_RW_DIRS 加入 _BLOCKED_VARS |
| 3 | 7f92f41e | 沙箱 HOME --bind 移到 _post_tmpfs_binds 之前 |
| 3b | 90f521db | _handled_targets 用实际 bind target（宿主机路径），不误跳 per-task bind |
| 4 | 4802b775 | per-task HOME 下补 `.cache/codeartsdoer` RO bind |

## 影响面分析

| 引擎 | 影响 | 说明 |
|------|------|------|
| codearts | 修复生效 | models-cache.json 在沙箱内可见，loadXCenterModels fallback 正常 |
| hermes | 不影响 | 无 `.cache/codeartsdoer` 依赖 |
| opencode | 不影响 | 无 `.cache/codeartsdoer` 依赖 |

代码改动仅在 `src/runtime/codearts.py` 的 `prepare_task_env` 方法内，新增一条 GRAPE_TASK_RW_DIRS JSON 条目。无硬编码路径（`_home = _os.path.expanduser("~")`，`_sandbox_home = env.get("HOME")`）。bwrap.py 的 `build_bwrap_argv` 读取 GRAPE_TASK_RW_DIRS 是通用遍历逻辑，不感知具体内容。

## 验证标准

| 检查项 | 标准 |
|--------|------|
| models-cache.json 可见 | 沙箱内 `$HOME/.cache/codeartsdoer/models-cache.json` 存在 |
| loadXCenterModels | fallback 到本地缓存，不抛异常 |
| stdout 输出 | text_len > 0，result 非空 |
| 任务完成 | exit=0, status=completed |
| hermes/opencode 无回归 | 三引擎并发验证，hermes/opencode 仍然通过 |

## 验证结果（194 机器，2026-10-08）

### 手动测试 C（per-task HOME + 修复后）
- EXIT_CODE: 0
- STDOUT_LINES: 3
- STDOUT[1] text: "你好！我是 CodeArts，由华为云码道开发的代码智能体。有什么可以帮你的吗？"
- STDERR_LINES: 0

### 真实 worker E2E（issue #51，VOD 阶段）
- task_id: t_ab71eb3e966e
- runtime_id: 900145 (codearts)
- status: completed
- text_len: 35
- result_text: `{"scene_id": "", "reason": "未配置场景"}`
- exit_code: 0
- 说明："未配置场景"是 VOD 阶段正常输出——hello-skill 仓库未配置开发场景，codearts 正确识别并输出 JSON

### 对比修复前
同 issue #51 的旧任务 t_05cd58e24d6d（修复前）：
- text_len: 0
- exit_code: 0
- result_text: 空
