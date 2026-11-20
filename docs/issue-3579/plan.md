# Issue #3579 Plan：pre-push 钩子运行时路径解析

## 变更点
| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/branch_guard.py` | 模板 2028-07；新增 `HOOK_MODULE_REL_PATH`、`_hook_module_relpath`、`_resolve_hook_interpreter`、`_hook_runtime_entry_code`；重写 `_existing_hook_usable`；`install_pre_push_hook` 去掉绝对路径 marker |
| `tests/test_issue_3579_branch_guard_runtime_path.py` | 新增解释器缺失/模块缺失/正常 fail-closed/存量升级回归 |
| `tests/test_issue_2028_branch_guard_fail_open.py` | 调整模板断言：不再固化 `sys.executable`，改校验相对模块路径 |
| `tests/runtime/test_issue_2292_branch_guard_canonical_lock.py` | 模块 marker 断言改为相对路径 |
| `tests/runtime/test_issue_2933_readonly_log_failopen.py` | 版本断言更新为 2028-07 |
| `tests/runtime/test_issue_993_branch_guard.py` | 假解释器改由 PATH 注入（运行时 `command -v python3`） |

## 运行时链路
1. shell 暂存 stdin → 执行 `pre-push.grape-local`（若有）。
2. `command -v python3` 失败：单行 `[branch-guard] WARNING` + `exit 0`。
3. `git rev-parse --show-toplevel` 得到当前 worktree 顶层，传给内联 Python。
4. 依次尝试 `<root>/<rel>`、`<cwd>/<rel>`，缺失则部署态包导入。
5. 加载/执行异常：fail-open；`hook_main` 显式返回 1：保持 fail-closed。

## 风险
| 风险 | 处理 |
|------|------|
| 系统 python3 无宿主依赖导致守卫长期降级 | 告警含模块名与修复指引；正常部署态可回退包导入 |
| 相对路径被篡改 | `_existing_hook_usable` 要求与 canonical rel 完全一致 |
| 旧模板残留 | 版本号递增 + 绝对 marker 判为不可用，安装时重写 |