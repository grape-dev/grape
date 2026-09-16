# Issue #172: codex --sandbox workspace-write 网络限制导致部分任务失败

## 问题概述

codex 引擎任务在 agent 执行过程中需要网络访问（如调 GitCode API 获取 Issue 详情、git fetch 拉 PR diff）时，被 codex CLI 自身的 `--sandbox workspace-write` 模式拦截，报 `CODEX_SANDBOX_NETWORK_DISABLED=1`，导致任务失败。

## 根因

`src/runtime/codex.py:53` 硬编码了 `--sandbox workspace-write`：

```python
args = ["exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check"]
```

codex CLI 的 `--sandbox workspace-write` 模式语义：允许读写工作区文件，但**默认禁止网络访问**。当 agent 在执行 bash 命令时发起网络请求（curl/git fetch/API 调用），会被 codex 自身沙箱拦截。

### 影响链路

```
grape _assemble_env 注入 BOT_GITCODE_TOKEN
  → agent prompt 驱动执行 bash 命令调 GitCode API
    → codex sandbox 拦截网络出站
      → agent 报「网络在沙箱中被禁用（CODEX_SANDBOX_NETWORK_DISABLED=1）」
        → 任务卡死或失败（exit=-9）
```

### 与 #168 的关系

| 维度 | #168 | #172 |
|------|------|------|
| 沙箱来源 | grape bwrap 文件系统隔离 | codex CLI 内部沙箱 |
| 影响范围 | 所有引擎 | 仅 codex 引擎 |
| 状态 | 已修复（default-off） | 本 issue 修复 |

即使 #168 修复（关 bwrap），codex 的网络限制依然存在。两者完全独立。

## 实际影响

events 表中 codex 引擎事件统计（截至 2026-09-15）：

| 状态 | 数量 | 占比 |
|------|------|------|
| completed | 29 | 67% |
| cancelled | 4 | 9% |
| failed | 10 | 23% |

10 个失败中明确由 codex sandbox 网络限制导致的：

- **seq 5299**（2026-09-14）：agent 尝试调 GitCode API 获取 Issue 详情，报「网络在沙箱中被禁用」，exit=-9
- **seq 5335**（2026-09-14）：PR review 时 worktree 内无 PR 变更，agent 试图 git fetch 远程，被网络限制阻塞

大部分 codex 任务能成功，因为 a2a 事件（review/spec/评级）的 prompt 已内联足够上下文，不需要运行时网络。

## 引擎对比

| 引擎 | 沙箱模式 | 网络限制 | 来源 |
|------|---------|---------|------|
| opencode | `--dangerously-skip-permissions` | 无 | opencode CLI |
| codearts | `--auto` + `sandbox/config.json network_policy=allow_all` | 无（grape 预写了 allow_all） | codearts CLI |
| hermes | 无 | 无 | — |
| codex | `--sandbox workspace-write` | **有（默认禁网）** | codex CLI |

## 修复方案

### 方案 A：放开网络（采用）

将 `--sandbox workspace-write` 改为 `--sandbox danger-full-access`，允许网络访问。

- 优点：简单一步到位
- 缺点：codex agent 能任意访问网络，无白名单控制

### 方案 B：codex 配置文件设网络策略（后续独立 issue）

检查 codex CLI 是否支持通过配置文件（`~/.codex/config.json` 或类似）设网络白名单，类似 codearts 的 `sandbox/config.json network_policy=allow_all` 模式。

### 方案 C：环境变量覆盖（后续独立 issue）

检查 codex CLI 是否支持通过环境变量（如 `CODEX_SANDBOX_NETWORK_DISABLED=0`）放开网络限制。

### 决策

采用方案 A。后续通过独立 issue 以网络白名单策略收紧（方案 B/C）。

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/runtime/codex.py` | `--sandbox workspace-write` → `danger-full-access` + 注释同步 |
| `src/runtime/agent.py` | `_PROBE_COMMANDS` codex 探活命令同步 |
| `src/observability/engine_probe.py` | 探活命令 + 文档注释同步 |
| `tests/test_runtime_codex.py` | 测试期望值同步 + 修复基线失败（补 `--skip-git-repo-check`） |
| `tests/test_engine_probe_codex.py` | 探活测试期望值同步 |

## 测试

- `test_runtime_codex` + `test_engine_probe_codex`：50/50 passed
- 扩展验证（含 `test_agent_executor` + `test_phase9`）：102/102 passed

## 关联

- #168（bwrap 沙箱文件系统隔离，独立问题，已修复）
- #64（沙箱隔离父 Issue）
- #94（codex 引擎映射缺失，已修复）
