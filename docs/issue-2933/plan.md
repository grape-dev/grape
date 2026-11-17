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
   - `LOG_FILE_DEGRADED` + `LOG_FILE_DEGRADED_REASON` 模块级标志（唯一保留的降级标志）；
   - 抽出 `_ensure_dir()` / `_warn_stderr()`：`os.makedirs` 失败只返回原因、不上抛，
     stderr 告警尽力而为；`LOG_DIR` 只定义一次（导入期与目录常量共用）；
   - 导入期 `os.makedirs(LOG_DIR)` + `TimedRotatingFileHandler` 构造失败统一走
     `try/except OSError`，失败时 `_file_handler=None`，stderr 直写一条中文提示
     （按阶段拼真实原因：目录不可写 / handler 构造失败，不含 traceback）；
   - `basicConfig` 的 handlers 列表剔除失效 handler，全空时兜底 `NullHandler`；
   - `WORKSPACE_DIR` 创建失败同样经 `_ensure_dir()` 降级并写 stderr，不再维护无消费方的
     `WORKSPACE_DIR_DEGRADED` / `LOG_DIR_DEGRADED` 标志。
2. `src/domains/runtime/engine/branch_guard.py`
   - shell 层 `cat > "$STDIN_TMP" || exit 1` → `if ! cat ... ; then` 告警单行（含 `--no-verify`）+ `exit 0`；
   - `_fail_open()` 与 `_hook_entry_code()` 告警统一含 `--no-verify`；
   - `HOOK_TEMPLATE_VERSION` → `2028-05`；
   - `install_pre_push_hook()` 失败路径（hooks 目录只读/`_hooks_dir` 为空）经
     `_hook_not_upgraded()` 记 `branch guard hook not upgraded ...`，文案按真实 `reason`
     拼接，日志失败时兜底写 stderr（双通道，禁止静默）。
3. `src/domains/runtime/engine/repogit.py`
   - `_install_branch_guard_hook()` 安装返回 False 时复用 `_hook_not_upgraded()`，
     日志 + stderr 双通道可见，不阻断建树。
4. 回归测试：只读目录导入（含 stderr 实际输出断言）、handler 构造失败、TMPDIR 不可写、
   只读 hooks 目录（makedirs 失败 + 写 pre-push 权限不足）、logger 失败 stderr 兜底、
   旧模板升级、三类 fail-closed 语义（含 `hook_main` 实际退出码 1）。
5. 文档：spec 显式标注「gitdir 只读时存量钩子无法重写；若同时 TMPDIR 不可写仍会阻断 push」
   的限制。

