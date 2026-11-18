# Issue #2292 — 任务拆解与验证记录（task）

## 任务拆解（TDD：RED → GREEN → REFACTOR）

| # | 任务 | 状态 | 产物 |
|---|------|------|------|
| 1 | 复现点确认：源码 3 处 `from src import lock`、`HOOK_DEP_MODULES = ("src.lock", ...)`、冒烟未覆盖 lock | 完成 | — |
| 2 | RED：新增 `tests/test_issue_2292_branch_guard_canonical_lock.py`（18 项），先跑出 8 项失败 | 完成 | 失败截图见下方记录 |
| 3 | GREEN：`branch_guard.py` 导入迁 canonical、`HOOK_DEP_MODULES` 对齐、模板版本 `2028-04 → 2292-01`、fail-open 修复指引 | 完成 | `src/domains/runtime/engine/branch_guard.py` |
| 4 | GREEN：`install-worker.sh` 冒烟自检覆盖 canonical lock / 旧别名 / 新布局守卫；otel 诊断路径纠正；拒绝升级附 `Fix:` 指引 | 完成 | `scripts/install-worker.sh` |
| 5 | 发布闭包注释如实化 | 完成 | `src/kernel/worker_package.py` |
| 6 | CI 接入回归（branch guard 步骤） | 完成 | `.github/workflows/windows-smoke.yml` |
| 7 | 既有用例对齐 `src.kernel.lock`（#2028 依赖缺失例） | 完成 | `tests/test_issue_2028_branch_guard_fail_open.py` |
| 8 | 回归测试全量核对 | 完成 | 见下 |

## 测试结果

RED（实现前，`tests/test_issue_2292_branch_guard_canonical_lock.py`）：

```
8 failed, 9 passed
FAILED test_branch_guard_source_has_no_legacy_top_level_lock_import
FAILED test_hook_dep_modules_point_to_canonical_layout
FAILED test_hook_template_version_is_bumped_past_2028_template
FAILED test_hook_entry_code_fail_open_with_repair_hint_when_lock_missing
FAILED test_hook_main_missing_dependency_reports_repair_hint
FAILED test_install_worker_smoke_checks_lock_alias_and_branch_guard
FAILED test_smoke_check_rejects_package_missing_kernel_lock
FAILED test_smoke_check_rejects_package_missing_branch_guard
```

GREEN（实现后）：

```
tests/test_issue_2292_branch_guard_canonical_lock.py        18 passed
tests/test_issue_2028_branch_guard_fail_open.py             (17 项) passed
tests/arch                                                41 passed
```

分支守卫相关全集（`tests/runtime/test_issue_993_branch_guard.py` +
`tests/runtime/test_issue_993_guard_state_prune.py` + `tests/arch/test_silent_except_ratchet.py`
+ 2028 + 2292）：

```
2 failed, 78 passed
```

两项失败为**基线漂移**，与本改动无关：`test_agent_linked_push_path_uses_guard` /
`test_direct_linked_push_path_uses_guard` 断言 `agent.py` / `event_pipeline.py` 调用
`branch_lease`，而本基线这两个模块未接入该调用（`grep branch_lease` 为空）；
#2147 在 PR #1275 的描述中记录了同一现象（其基线同样存在该 2 项）。为避免超出
Issue 范围，本次不改动 pipeline 集成。

`tests/runtime/test_worker_package.py`：7 failed / 41 passed，失败项全部为
`ModuleNotFoundError: No module named 'sqlalchemy'`（本地环境缺依赖，非代码问题）。

## 验证要点（对应验收标准）

1. **canonical 导入**：源码 `from src import lock` 绝迹；`_lease_holder` /
   `acquire_branch_lease` / `release_branch_lease` 实跑成功（真 DB 租约）。
2. **缺模块 fail-open**：在临时树中真实加载 `branch_guard` 且只放
   `src/kernel/__init__.py`（无 `lock.py`）→ 钩子进程 `exit 0`，stderr 同时出现
   `[branch-guard] WARNING`、`fail-open`、`src.kernel.lock`、`scripts/install-worker.sh`
   修复指引，且无裸 traceback。
3. **真实违规不放过**：租约冲突、PR 已合并两条路径仍 `return 1`。
4. **存量钩子自愈**：模板版本 `2292-01` 写入钩子；`_existing_hook_usable` 对新钩子
   返回 True、repo_key 不一致返回 False（触发重写）。
5. **冒烟自检**：缺 `src/kernel/lock.py` → `smoke_rc=1` 且输出指认模块；缺
   `branch_guard.py` → 同样拒绝；齐全 → `smoke_rc=0`（正控，防夹具假阳性）。
6. **发布闭包**：`MANIFEST` 含 `src/kernel/` 与 `src/__init__.py`；注释明确不要再加
   不存在的 `src/lock.py`。

## 与 #2147 的合入约定

两个 PR 修改同一批文件同一批位置，**二选一先合**，另一侧 rebase 后取并集：
- 导入路径 / `HOOK_DEP_MODULES` / 冒烟自检：两单内容等价（以任一侧为准）；
- 钩子模板与 `_fail_open` 修复指引、冒烟旧别名校验：仅本单有，rebase 时保留；
- `HOOK_TEMPLATE_VERSION`：取更高者 `2292-01`（#1275 rebase 时需同步，否则存量钩子
  会被两侧版本互相降级重写）。
## QA 复审处置（2026-11-16）

- **基线对齐**：分支已 rebase 到当前 `arch-weekly-11-zj`（原分支基于 #2281 谱系，
  diff 混入 #1982/#2041/#2133 等 53 个文件）。rebase 后 PR #1331 diff 收敛为
  9 个文件，全部属于本 Issue 范围；`pulls get` 返回 `mergeable=true`、
  `conflict_passed=true`、`non_ff_passed=true`。
- **可选优化已落实**：冒烟诊断改校验 canonical `src.kernel.otel`（减少安装期对
  insight 域的耦合）；`HOOK_REPAIR_HINT` 明确 `--no-verify` 仅在守卫故障时临时使用、
  不可日常化；单测文件补结尾换行。
- **与 #2147 并单**：确认 PR #1275 仍基于 #2010 前扁平布局（引用 `src.config` /
  `src.observability.otel` 在当前基线不存在），以 PR #1331 为唯一实现。Coding CLI
  的 `pulls update` 不支持 `state`（无法由 Agent 关闭 PR），需维护者在 GitCode 手动
  关闭 #1275；#2147 待 #1331 合入后 rebase 取并集。
- **回归**：`test_issue_2292_branch_guard_canonical_lock.py` + 2028 用例 35 passed；
  分支守卫全集 78 passed / 2 failed（`agent.py`/`event_pipeline.py` 未接入
  `branch_lease` 的基线漂移，与 #2147 记录一致，不在本 Issue 范围）。
- **端到端**：安装指向本分支 `branch_guard` 的 pre-push 钩子后，`grape git push`
  未使用 `--no-verify` 成功推送；本机 DB 缺 `distributed_locks` 表（#2291）时钩子
  按设计 fail-open 放行并打印 WARNING + 修复指引。
