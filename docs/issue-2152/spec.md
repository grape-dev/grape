# Spec: dsh 沙箱内 GRAPE_AUTH_FILE 悬空修复

## Objective

修复 dsh 引擎的任务级凭据穿透：`GRAPE_AUTH_FILE` 指向的文件必须落在工位 `cwd`
（bwrap 沙箱 rw-bind 可见），使 dsh 执行 Coding/QA 时 `grape` CLI 可认证并 push。

## Scope

- `DshRuntime.prepare_env(env, cwd="") -> str | None`：有 `cwd` 时写入
  `<cwd>/.grape-sess-<token_hex(4)>.tok`，权限 `0600`，返回路径并设置
  `env["GRAPE_AUTH_FILE"]`；无 `cwd` 时回退 `mkstemp` 并记录 warning。
- 有 `cwd` 时通过 `git -C <cwd> rev-parse --git-path info/exclude` 注册
  `.grape-sess-*.tok`，兼容 linked worktree；空路径或注册失败均记录 warning。
- `stream_agent` 通用 CLI 分支把 `cwd` 传给 `prepare_env`，并在生成器收尾的
  `finally` 中清理 `cwd` 下的 `.grape-sess-*.tok`；无 `cwd` 时清理返回的 auth file。
- 不改变 dsh 的 `/TOKEN/i` scrub；不改变其他引擎行为；不引入依赖；token 不进入
  命令行、日志或 git 跟踪文件。

## Commands

```bash
python3 -m pytest tests/runtime/test_dsh_runtime.py -q
python3 -m pytest tests/runtime/test_executor_token_env.py -q
```

## Project Structure

- `src/domains/runtime/engine/dsh.py`：dsh runtime 与 token file 准备逻辑。
- `src/domains/runtime/agents/executor.py`：通用 CLI 后端执行与生命周期清理。
- `src/domains/runtime/agents/aishell_acp.py`：同构参考实现，不预期改动。
- `tests/runtime/test_dsh_runtime.py`：本修复的回归契约。
- `docs/issue-2152/`：本 Issue 的需求、方案与任务文档。

## Testing Strategy

使用 pytest，针对 `prepare_env` 和 `stream_agent` 两个 seam 做行为测试：

- 有 `cwd`：文件位于 `cwd`、权限 `0600`、内容正确、`git status` 不可见。
- 无 token：不生成文件。
- 无 `cwd`：回退系统临时目录并记录 warning。
- 任务结束：文件被清理，异常路径也执行收尾。
- 无 `prepare_env` 的其他后端：不产生额外 env 变化。

## Boundaries

- Always：先写失败测试；提交前检查增量 diff；保留现有警告与错误归因语义。
- Ask first：修改 `aishell_acp.py` 或引入新依赖。
- Never：把 token 写入日志、命令行、跟踪文件或提交内容。

## Success Criteria

- dsh 任务内 `GRAPE_AUTH_FILE` 指向 `cwd` 下可读文件，且文件退出后不存在。
- token 文件不进入 `git status`，且 `.git/info/exclude` 包含 `.grape-sess-*.tok`。
- 无 token 不生成文件；无 `cwd` 回退 temp 且 warning 可见。
- 其他引擎 env 行为不变；相关 pytest 全部通过。

## Open Questions

无。Issue #2152 的 Spec 已给出明确实现与验收边界。
