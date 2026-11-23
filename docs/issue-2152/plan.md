# Implementation Plan: dsh 任务级凭据穿透修复

## Overview

把 dsh 的 token 文件从宿主机 `/tmp` 移动到工位 `cwd`，通过 bwrap 的 rw-bind
保证沙箱内可读；同时在仓库级 exclude 与 executor 收尾清理之间建立双保险。

## Architecture Decisions

- `prepare_env` 增加可选 `cwd` 参数，保持无 `cwd` 调用的向后兼容。
- token 文件使用 `os.open(..., O_EXCL, 0o600)` 原子创建，避免覆盖与权限窗口。
- exclude 路径使用 `git rev-parse --git-path info/exclude`，兼容 linked worktree。
- 清理放在 `stream_agent` 生成器的 `finally`，覆盖正常结束、异常与超时终止。
- 仅对实现 `prepare_env` 的通用 CLI 后端启用收尾清理，ACP 专用路径保持原样。

## Task List

### Phase 1: Contract

- [ ] Task 1: 更新 `test_prepare_env_*` 签名并新增 cwd 落盘、exclude、无 cwd warning 契约。
- [ ] Task 2: 新增 executor 生命周期清理与其他后端 env 不变契约。

### Checkpoint: Red

- [ ] 新测试在当前实现上失败，证明契约先于实现。

### Phase 2: Implementation

- [ ] Task 3: 实现 `DshRuntime.prepare_env(env, cwd)` 与安全落盘/exclude。
- [ ] Task 4: 通用 CLI 分支传递 `cwd` 并接入 `finally` 清理。

### Checkpoint: Green

- [ ] `tests/runtime/test_dsh_runtime.py` 全绿。
- [ ] `tests/runtime/test_executor_token_env.py` 全绿。

### Phase 3: Verification

- [ ] Task 5: 运行运行时相关回归、检查 git status 与增量 diff。
- [ ] Task 6: 完成 5 轴自审查与开源前置增量检查。

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 异常/超时导致 token 残留 | High | `${cwd}/.grape-sess-*.tok` 注册 exclude，并在生成器 `finally` 清收 |
| linked worktree exclude 写错位置 | Medium | 使用 `--git-path info/exclude` |
| 改动泛化到其他引擎 | Medium | ACP 分支不动；仅通用 CLI 分支按 `prepare_env` 返回值清理 |
| 测试依赖旧路径导入失败 | Medium | 同步修正测试 helper 的模块导入与 mock 目标 |

## Open Questions

无。
