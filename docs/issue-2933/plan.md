# Issue #2933 方案设计

## 设计原则

把「环境只读」与「推送违规」在代码里彻底分开：环境/依赖/守卫自身异常 → fail-open；
只有明确违规才 fail-closed 返回 1。

| 决策 | 选择 | 理由 |
|------|------|------|
| 降级粒度 | 只剔除文件 handler，保留 stderr | 可观测性损失最小 |
| 降级告警通道 | 直接写 `sys.stderr` | 坏掉的正是文件 handler，经 logger 有递归/吞掉风险 |
| 只读 gitdir 后果 | 导入期降级 + 显式「未升级」提示 | 隐式 `--no-verify` 等于关闭分支保护 |
| 模板升级方式 | 递增 `HOOK_TEMPLATE_VERSION` 到 `2028-05` | 指纹不变则存量 worktree 永不升级 |
| stdin 暂存失败 | stderr 单行告警 + `exit 0` | 环境只读 ≠ 推送违规，且不误吞 stdin |

## 实现路径

1. `src/kernel/config.py`
   - `LOG_FILE_DEGRADED` + `LOG_FILE_DEGRADED_REASON` 模块级标志；
   - `os.makedirs(_LOG_DIR)` + `TimedRotatingFileHandler` 构造包 `try/except (OSError, PermissionError)`，
     失败时 `_file_handler=None`，stderr 直写一条中文提示（不含 traceback）；
   - `basicConfig` 的 handlers 列表剔除失效 handler，全空时兜底 `NullHandler`；
   - `WORKSPACE_DIR` / `LOG_DIR` 的 `os.makedirs` 同样包 `try/except`，只记
     `WORKSPACE_DIR_DEGRADED` / `LOG_DIR_DEGRADED` 标志，不抛。
2. `src/domains/runtime/engine/branch_guard.py`
   - shell 层 `cat > "$STDIN_TMP" || exit 1` → `if ! cat ... ; then` 告警单行（含 `--no-verify`）+ `exit 0`；
   - `_fail_open()` 与 `_hook_entry_code()` 告警统一含 `--no-verify`；
   - `HOOK_TEMPLATE_VERSION` → `2028-05`；
   - `install_pre_push_hook()` 失败路径（hooks 目录只读/`_hooks_dir` 为空）经 `_LazyLog`
     记 `branch guard hook not upgraded ...`，不再静默。
3. `src/domains/runtime/engine/repogit.py`
   - `_install_branch_guard_hook()` 在安装返回 False 时额外向 stderr 打印一次可操作提示。
4. 回归测试：只读目录导入、handler 构造失败、TMPDIR 不可写、只读 hooks 目录安装失败、
   旧模板升级、三类 fail-closed 语义。