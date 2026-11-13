# Issue #2028 Spec：pre-push 分支守卫运行时缺 lock 模块，push 确定性被阻

> 目标分支：`arch-weekly-11-zj` | 优先级：P0 | 类型：bug

## 目标

修复 `grape-branch-guard` 在 worker 运行时的 `ImportError: cannot import name lock from src`：

1. `src/lock.py` 未随 worker 包下发 → `acquire_branch_lease` 内 `from src import lock` 失败；
2. `hook_main` 内部异常未被钩子模板 fail-open 捕获 → 守卫自身缺陷升级为 fail-closed，所有 push 被拒。

恢复既定语义：**守卫自身异常 fail-open（放行 + stderr 告警），真实违规 fail-closed（返回 1）。**

## 范围

### In Scope

1. `src/kernel/worker_package.py` MANIFEST 补齐顶层 `src/lock.py`。
2. `scripts/release_pack.py` `pack_tools()` manifest_files 同步补齐 `src/lock.py`。
3. `src/domains/runtime/engine/branch_guard.py`：
   - 钩子模板将模块加载与 `hook_main()` 一并纳入 try/except（fail-open + 可定位告警）；
   - 新增 `HOOK_TEMPLATE_VERSION` 指纹，`install_pre_push_hook` 对旧版/缺失指纹的存量钩子重装；
   - 修正钩子 `module_path`：按 #699 迁移后的 canonical 布局
     `src/domains/runtime/engine/branch_guard.py` 定位（原 `src/runtime/` 已不存在）。
4. `src/kernel/lock.py`：`acquire_lock/renew_lock/release_lock` 支持可选 `holder` 参数。
   branch_guard 以 `task_id:进程holder` 作为租约持有者，旧实现无此参数会 `TypeError`，
   租约无法获取（`src.lock` 仅是该模块的兼容别名，二者共享同一实现）。
5. 回归测试：发布闭包含 `lock.py`、fail-open（异常/缺模块）、真实违规仍 fail-closed、
   存量钩子版本升级、幂等安装。

### Out of Scope

- #1923（remote_drift 陈旧租约）、#2024（工作区 git 目录只读）。
- 旧分支提交 `9dfdae101` / `4c04e50d62` 直接 cherry-pick（按当前布局重写）。

## 验收标准

- [x] 新 worker 包内存在 `src/lock.py`，`from src import lock` 可导入
  （`test_worker_package_manifest_includes_top_level_lock` /
  `test_release_pack_manifest_includes_top_level_lock`，MR #1216 实跑通过）
- [x] 无 lock 环境下 `hook_main` ImportError 不阻断 push（exit 0 + stderr 明确告警）
  （`test_hook_entry_code_fail_open_when_module_missing` /
  `test_hook_entry_code_fail_open_when_spec_is_none` /
  `test_hook_main_missing_dependency_reports_locatable_error`，17 项实跑通过；
  QA 第 4 轮另以生成钩子代码三场景行为实证）
- [x] 真实拦截（租约争用 / PR merged / 远端漂移）仍返回非零
  （`test_hook_main_still_fail_closed_on_real_block` +
  `test_hook_main_release_failure_keeps_block_verdict`：finally 释放异常不翻转判定）
- [x] `HOOK_TEMPLATE_VERSION` 不匹配的存量钩子被重装；钩子 module_path 实测可加载
  （`test_install_rewrites_outdated_guard_hook` +
  `test_install_is_idempotent_for_current_template` +
  `test_existing_hook_usable_accepts_freshly_installed_hook`：新装钩子幂等校验为
  True，失效路径三用例覆盖解释器/模块路径/repo_key；module_path 指向
  canonical 布局由 `test_hook_template_module_path_points_to_existing_canonical_file` 断言）
- [ ] coding 实跑 `grape git push`（不带 `--no-verify`）成功且租约正常获取
  （AC5：QA 环境无法复现真实 worker 部署链路，转部署阶段验证；基线
  `c84a775f7` 后 acquire_lock holder 参数已在位，pre-push 守卫在 #2041 变基
  推送中已实际执行通过——#1228 PR 描述）
- [x] 现有相关测试通过 + 新增测试覆盖
  （`tests/test_issue_2028_branch_guard_fail_open.py` 17 项 +
  `tests/runtime/test_worker_package.py` 29 项实跑通过；
  `tests/test_issue_993_branch_guard.py` 与基线差集对照零新增失败，且
  holder 修复顺带转绿 4 条基线红用例）

关联：#1917（同根因）| 范围外：#1923、#2024