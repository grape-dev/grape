# Task: Issue #752

## M1 合入冲突标记扫描门禁
- [x] T1 `src/api/merge_gate.py`：扫描函数 + run_merge_gate + record_gate_log
- [x] T2 `src/api/db_setup.py`：`grape_t_merge_gate_log` DDL + 注册 + safe 迁移
- [x] T3 `git_repo_api.py::merge_pull`：合入前阻断/告警 + 合入后告警 + 合入日志

## M2 复测/自动提交保真校验
- [x] T4 `src/runtime/worktree_guard.py`：暂存扫描 + 指纹快照/校验
- [x] T5 `repogit.ensure_issue_worktree`：任务开始保存基线
- [x] T6 推送守卫：agent.py / event_pipeline.py linked PR 流 commit 前硬命中阻断
- [x] T7 清理保真：两端 cleanup 卸载前指纹比对告警

## M3 测试
- [x] T8 tests/test_issue_752_merge_gate.py（门禁命中/放行/误报/日志）
- [x] T9 tests/test_issue_752_worktree_guard.py（推送阻断/指纹/排除）
- [x] T10 回归 issue-558 merge 透传 + repogit 基线行为

## 验收
- [x] AC1/AC2/AC3 单测覆盖；全部现有测试通过
