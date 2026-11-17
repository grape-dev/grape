# Issue #2933 Spec：只读日志路径下 pre-push 分支守卫不得阻断 push

> 来源：Issue #2933（user feedback #8289）｜目标分支 `weekly-mys`

## 背景

容器/沙箱环境把会话 worktree 的 gitdir 与 `data/logs` 挂成只读时，
`src/kernel/config.py` 在**模块导入期**执行 `os.makedirs(LOG_DIR)` 并构造
`TimedRotatingFileHandler(data/logs/devops-bot.log)`，两处均无 `try/except`：

1. `import src.kernel.config` 抛 `OSError(Read-only file system)`；
2. pre-push 分支守卫（钩子模板 `2028-04`）在导入守卫模块时即崩溃，钩子非零退出；
3. git 在鉴权前拒绝 push（`failed to push some refs`），用户拿不到 `--no-verify` 指引。

另有一处 fail-closed 漏点：钩子 shell 包装层 `cat > "$STDIN_TMP" || exit 1`，
`TMPDIR` 不可写时同样直接拒推，走不到 Python 侧的 fail-open。

同一条钩子链上 #2812（`src.lock` 缺失）/ #2855（`distributed_locks` 缺表）/
#2923（缺 `src.vcs`）为不同失败面，本单根因=**日志写入路径只读未降级**。

## 目标

- 只读日志目录下 `import src.kernel.config` 退出码 0，日志降级到 stderr；
- 只读 `TMPDIR` 时钩子 shell 层 fail-open 放行，单行告警含 `--no-verify` 指引；
- 只读 hooks 目录无法升级钩子时必须可观测（不静默返回 False）；
- 真实违规（PR merged/closed、租约被占、远端漂移）仍 fail-closed 阻断。

## 范围

### In Scope
- `src/kernel/config.py`：导入期日志初始化（`os.makedirs` + 文件 handler 构造）降级；
  `WORKSPACE_DIR` / `LOG_DIR` 的 `os.makedirs` 同类收敛。
- `src/domains/runtime/engine/branch_guard.py`：shell 层 stdin 暂存失败 fail-open、
  告警文案含 `--no-verify`、`HOOK_TEMPLATE_VERSION` → `2028-05`、安装失败可观测。
- `src/domains/runtime/engine/repogit.py`：worktree 准备阶段安装失败时 stderr 提示一次。
- `tests/runtime/test_issue_2933_readonly_log_failopen.py`：回归用例。

### Out of Scope
- 不改 #2812 / #2855 / #2923 的失败面；
- 不改分支守卫判定语义（租约/PR 状态/远端漂移规则）；
- 不做日志落盘重定向（不引入 journal/不猜其它可写目录）。

## 已知限制

- **gitdir 只读时存量钩子无法重写**：若 hooks 目录位于只读 gitdir，存量
  `2028-04`（及更早）钩子无法升级为 `2028-05` fail-open 模板；此时若环境同时
  `TMPDIR` 不可写，旧钩子 shell 层 `cat ... || exit 1` 仍会阻断 push，**走不到**
  Python 侧 fail-open。本单的新模板 fail-open 只覆盖 gitdir 可写（钩子能升级）的场景。
- 上述场景下由 `_hook_not_upgraded()` 输出「守卫未升级」告警（日志 + stderr 双通道），
  提供 `--no-verify` 等可操作指引，但不会改变旧钩子的阻断行为。

## 验收标准

- [x] 日志目录只读时 `python -c "import src.kernel.config"` 退出码 0，stderr 仅一条降级提示、无 traceback。
- [x] 文件 handler 构造失败时 `LOG_FILE_DEGRADED=True` 且 `log.info` / `log.warning` 仍实际输出 stderr。
- [x] `TMPDIR` 不可写时钩子 shell 层退出码 0，stderr 含 `--no-verify`。
- [x] hooks 目录只读（makedirs 失败或写 pre-push 权限不足）时 `install_pre_push_hook()` 返回 False 且留下真实原因告警。
- [x] 存量 `2028-03` 旧模板可被重写为 `2028-05`。
- [x] PR merged、租约被占、远端漂移三类 fail-closed 用例保持阻断（含 `hook_main` 实际退出码 1）。
