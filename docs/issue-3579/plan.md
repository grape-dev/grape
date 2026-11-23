# Issue #3579 Plan：pre-push 钩子运行时路径解析

## 变更点
| 文件 | 改动 |
|------|------|
| `src/domains/runtime/engine/branch_guard.py` | 模板 2028-08；`_hook_module_relpath`/`_hook_runtime_entry_code`（部署态优先加载链）；新增 `_stable_module_root`（issue 工作树安装不记录根）；`_resolve_hook_interpreter` 改为 GRAPE_PYTHON → VIRTUAL_ENV → .venv → PATH 顺序；`_existing_hook_usable` 三 marker 校验、存在性按记录根统一 |
| `tests/runtime/test_issue_3579_branch_guard_runtime_path.py` | 解释器缺失/模块缺失/正常 fail-closed/存量升级回归 + QA 复审用例（部署态优先 2 例、sys.path 清洗、稳定根分类 3 例、解释器顺序 3 例、校验路径统一） |
| `tests/test_issue_2028_branch_guard_fail_open.py` | 模板断言：不再固化 `sys.executable`，改校验相对模块路径 |
| `tests/runtime/test_issue_2292_branch_guard_canonical_lock.py` | 模块 marker 断言改为相对路径 |
| `tests/runtime/test_issue_2933_readonly_log_failopen.py` | 版本断言更新为 2028-08 |
| `tests/runtime/test_issue_993_branch_guard.py` | 假解释器由 PATH 注入；清理 GRAPE_PYTHON/VIRTUAL_ENV 保证确定性 |
| `.github/workflows/windows-smoke.yml` | linux-backend 接入 branch_guard 全量回归（QA 复审·变更验证 Required；993 两个基线即失败的 linked-push 用例显式 deselect） |

## 运行时链路
1. shell 暂存 stdin → 执行 `pre-push.grape-local`（若有）。
2. `git rev-parse --show-toplevel` 得到当前 worktree 顶层（ROOT）。
3. 解释器解析：`GRAPE_PYTHON` → `VIRTUAL_ENV/bin/python3` → `ROOT/.venv/bin/python3`
   → `command -v python3`；全部失败：单行 `[branch-guard] WARNING` + `exit 0`。
4. 模块加载（部署态优先）：`<module_root>/<rel>` → `<ROOT>/<rel>`/`<cwd>/<rel>`
   → 部署态包导入（导入前移除 `sys.path` 的 `''`/cwd，插入 module_root）。
5. 加载/执行异常：fail-open；`hook_main` 显式返回 1：保持 fail-closed。

## 风险
| 风险 | 处理 |
|------|------|
| 记录的稳定根随 worker 卸载失效 | `_existing_hook_usable` 按记录根校验失效即重写；运行时自动降级 worktree 兜底链，守卫不中断 |
| issue 工作树内安装误记录回收路径 | `_stable_module_root` 按 `issues/<n>/<agent>` 布局与安装目标一致性双重判定，命中不记录 |
| 系统 python3 无宿主依赖导致守卫长期降级 | 解释器优先 worker 同源来源（GRAPE_PYTHON/venv）；告警含模块名与修复指引 |
| 相对路径被篡改 | `_existing_hook_usable` 要求与 canonical rel 完全一致 |
| 旧模板残留 | 版本号递增（2028-08）+ 绝对 marker/缺 root marker 判为不可用，安装时重写 |
