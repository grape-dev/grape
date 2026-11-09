# Task List: Issue #415

## Task 1: get_issue_scene 空值回退仓库默认场景
**Files:** src/api/session_store.py, tests/test_pipeline_issue182.py, tests/test_issue_415_scene_binding.py
**Acceptance:**
- [ ] issue 无场景 + 仓库 dev_scene 存在 → 返回 (dev_scene, 0)
- [ ] 仓库无 dev_scene → 保持 ("", 0)（不破坏智能推荐）
- [ ] PR 伪 id 分支不受影响

## Task 2: create_task_via_issue 无显式场景写入仓库默认场景
**Files:** src/api/issues_center.py, tests/test_issue_415_scene_binding.py
**Acceptance:**
- [ ] body.scene_id 为空/vod-auto 且仓库有 dev_scene → set_issue_scene 写入
- [ ] 显式 scene_id 仍按原逻辑写入
- [ ] 仓库无 dev_scene → 不写，走智能推荐

## Task 3: _query_tasks 空场景回退仓库默认场景展示
**Files:** src/api/issues_center.py, tests/test_issue_415_scene_binding.py
**Acceptance:**
- [ ] 无场景 issue → scene_id=仓库默认场景、scene_source=repo_default、scene_name 正确

## Task 4: 存量补绑脚本
**Files:** scripts/backfill_issue_scene.py, tests/test_issue_415_scene_binding.py
**Acceptance:**
- [ ] 默认 dry-run 列出无场景 Issue
- [ ] --apply 按仓库默认场景补绑（#406 验证样本）

## Task 5: 自审查 + 提交建 PR
**Acceptance:**
- [ ] 自审查 5 轴通过
- [ ] 相关测试通过
- [ ] PR 已创建并评论告知
- [ ] A2A QA