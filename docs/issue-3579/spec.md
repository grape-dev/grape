# Issue #3579 Spec：pre-push 守卫钩子运行时解析解释器与模块路径

## 背景
`install_pre_push_hook` 安装共享 `hooks/pre-push` 时，把 `sys.executable` 与守卫模块
绝对路径固化为 marker。安装若发生在 `issues/<n>/coding/.venv` 内，worktree 回收后
解释器路径失效，钩子以 exit 127 直接阻断 push；该失败发生在 shell 层，内联 Python
的 `try/except` fail-open 尚未执行。

## 目标
- 钩子不固化任何 in-flight worktree 绝对路径。
- 解释器、守卫模块任一引导失败都 fail-open（exit 0 + 单行可定位告警）。
- 正常环境下分支租约、PR merged、远端漂移校验保持 fail-closed。
- 存量钩子在 `ensure_issue_worktree` 安装路径上被识别并升级。

## 方案
1. 模板只写入 `# hook-module-rel: src/domains/runtime/engine/branch_guard.py` 与 repo_key。
2. shell 层运行时 `PY="$(command -v python3 2>/dev/null || true)"`，为空即 exit 0 告警。
3. 内联 Python 以当前 push worktree 顶层（`git rev-parse --show-toplevel`）拼接相对
   模块路径；缺失时退回部署态 `import src.domains.runtime.engine.branch_guard`；
   spec/loader/import/hook_main 任一异常统一 fail-open。
4. `_existing_hook_usable` 校验 repo_key、相对模块路径、解释器可解析性与模块文件存在，
   任一不可用即重写；旧版绝对 marker 模板一律视为不可用。
5. `HOOK_TEMPLATE_VERSION` 升级为 `2028-07`。

## 验收
- [x] 解释器缺失 → exit 0 + 单行告警，无 127。
- [x] 模块缺失 → exit 0 + 单行告警。
- [x] 正常路径 → 守卫异常/违规仍返回 1（fail-closed）。
- [x] 模板不含 `sys.executable` / 守卫模块绝对路径。
- [x] 存量旧模板在安装时被重写升级。