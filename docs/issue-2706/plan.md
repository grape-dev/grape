# Plan: Issue #2706 — 租约层 `_lock_module()` 收敛点 + fail-open

## 改动面

| # | 文件 | 类型 | 内容 |
|---|------|------|------|
| 1 | `src/domains/runtime/engine/branch_guard.py` | 修改 | 新增 `_lock_module()` 收敛点（惰性导入 + 异常 WARNING 返回 None）；`_lease_holder` / `acquire_branch_lease` / `release_branch_lease` 三处改为经收敛点取 lock；acquire 在 lock 不可用时返回 `{"ok": True, "degraded": True, "reason": "guard_lock_unavailable", ...}` |
| 2 | `tests/test_issue_2706_branch_guard_lease_fail_open.py` | 新增 | RED→GREEN 回归：lock 导入失败三函数 fail-open、正常路径不回归、真实违规仍阻断、源码级收敛点断言 |
| 3 | `.github/workflows/windows-smoke.yml` | 修改 | linux-backend「branch guard」步骤接入本 Issue 用例 + 守卫模块 import 冒烟（`python -c "import src.domains.runtime.engine.branch_guard"`） |
| 4 | `docs/issue-2706/runbook.md` | 新增 | `--no-verify` 标注为守卫故障期临时规避；发布包升级路径（重装 worker 包补齐 `src/kernel/lock.py`） |

## 实施顺序（TDD）

1. RED：新增用例（lock 导入失败 → acquire ok=True+degraded / release 不抛 / holder=None；
   builtins.__import__ 级模拟真实部署形态；源码断言直接导入只存在于 `_lock_module`）；
2. GREEN：实现 `_lock_module()` + 三函数接入；
3. REFACTOR：核对 WARNING 文案含模块名 + 修复指引（复用 `HOOK_REPAIR_HINT`）；
4. 回归：`tests/test_issue_2292_branch_guard_canonical_lock.py`、
   `tests/test_issue_2028_branch_guard_fail_open.py`、`tests/runtime/test_issue_993_branch_guard.py` 全绿；
5. CI 接入 + runbook 文档。

## 验证

- `python -m pytest tests/test_issue_2706_branch_guard_lease_fail_open.py tests/test_issue_2292_branch_guard_canonical_lock.py tests/test_issue_2028_branch_guard_fail_open.py tests/runtime/test_issue_993_branch_guard.py -v`
- 冒烟：`python -c "import src.domains.runtime.engine.branch_guard"`（CI 步骤内含）。

## main 布局同型修复指引（本分支不携带，PR body 引用）

main 布局 `src/runtime/branch_guard.py` 235/252/275 三处 `from src import lock as _lock`
改法相同：抽 `_lock_module()`（`try: from src import lock as _lock; except Exception:
WARNING + None`），三函数接入；`ok: True + degraded` 降级返回语义一致。另需在 main
布局发布 MANIFEST 补 `src/lock.py`（旧布局无 `src/__init__.py` 别名机制，随包下发实体文件）。
