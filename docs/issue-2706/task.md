# Task: Issue #2706

- [x] T1 需求理解 + 双布局现状核验（spec.md）
- [x] T2 spec/plan 文档输出
- [x] T3 RED：`tests/test_issue_2706_branch_guard_lease_fail_open.py`（合入侧增补 `tests/test_issue_2706_branch_guard_lease_fallback.py`：端到端缺模块部署树 + 降级不吞真实阻断）
- [x] T4 GREEN：`_lock_module()` 收敛点 + 三租约函数接入 fail-open（importlib 实现 + quiet 参数防重复告警 + stderr/log 双通道）
- [x] T5 回归：2292/2028/2314/worktree_reset 守卫用例全绿（993 两项基线失败为存量环境问题，与本修复无关）
- [x] T6 CI：linux-backend 接入本 Issue 用例（fail_open + fallback 两文件）+ 守卫/lock import 冒烟；worker_package 包内容断言补 lock.py/branch_guard.py 入包固化
- [x] T7 runbook.md：`--no-verify` 标注临时规避 + worker 包升级路径；deployment-runbook.md 故障对照表同步补行；main 布局同型补丁交付 docs/issue-2706/main-layout-patch.md
- [x] T8 自审查（五轴）→ 建 PR → Issue 评论 → A2A QA（PR #1491，QA 首轮+复审均通过，仅余文档 Optional 已在本轮修正）

## 验收核对（对照 Issue AC）

- [x] 缺 lock 模块：push 正常放行 + 可定位 WARNING，无 ImportError 上抛
- [x] 正常环境租约 acquire/renew/release 语义不回归
- [x] 守卫 import 冒烟纳入 CI 门禁
- [x] 守卫文档 fail-open+告警 契约与实现一致
- [x] 新增测试覆盖缺模块降级分支
