# Implementation Plan: Issue #415 对话创建 Issue 场景绑定修复

## Overview
修复 ChatAgent/VOD 对话通道创建 Issue 未绑场景、详情页「流水线场景」显示 -- 的问题。
三层修复：创建侧写入场景（缺省回退仓库默认场景）+ 展示侧 get_issue_scene/_query_tasks 空值回退 + 存量补绑脚本。

## Architecture Decisions
- `get_issue_scene` 空值回退 `managed_repos.dev_scene`：与 `_resolve_scene_id`/repogit/prompt_builder 既有「仓库默认场景」语义一致（PR 伪 id 分支已提前返回，不受影响）。
- `create_task_via_issue` 无显式场景（空 / vod-auto）时写入仓库默认场景：治本，使新 Issue 从创建即绑定场景。
- `_query_tasks` 空场景 → 仓库默认场景填充 scene_id/scene_name：任务中心详情/列表展示修复。
- 补绑脚本遵循 scripts/backfill_issue_user_id.py 模式：默认 dry-run，`--apply` 写库。

## Task List

### Phase 1: 展示侧回退
- [x] Task 1: `get_issue_scene` 空值回退仓库默认场景（src/api/session_store.py）
  - Acceptance: issue 无场景 + 仓库 dev_scene 存在 → 返回 (dev_scene, 0)
  - Verify: tests/test_pipeline_issue182.py 更新 + 新单测

### Phase 2: 创建侧写入
- [x] Task 2: `create_task_via_issue` 无显式场景 → 写入仓库默认场景（src/api/issues_center.py）
  - Acceptance: body.scene_id 为空/vod-auto 且仓库有 dev_scene → set_issue_scene 写入
  - Verify: 新单测 mock set_issue_scene/get_repo_scene

### Phase 3: 列表/详情展示
- [x] Task 3: `_query_tasks` 空场景 → 仓库默认场景展示（src/api/issues_center.py）
  - Acceptance: 无场景 issue 返回 scene_id=仓库默认场景、scene_source=repo_default、scene_name 正确
  - Verify: 新单测

### Phase 4: 存量补绑脚本
- [x] Task 4: `scripts/backfill_issue_scene.py`（默认 dry-run，--apply 写库）
  - Acceptance: dry-run 列出无场景 Issue；--apply 按仓库默认场景补绑
  - Verify: 脚本 dry-run 测试

### Phase 5: 自审查 + PR
- [ ] Task 5: 自审查、全量相关测试、更新既有断言、提交并建 PR

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 存量误绑错误场景 | Med | 补绑脚本默认 dry-run 输出清单 |
| 改 get_issue_scene 影响既有调用方 | Med | 仅补空值回退，PR 伪 id/既有 scene 分支不变；跑相关回归测试 |
| Behavior 变更导致旧测试失败 | Low | 更新 test_pipeline_issue182 旧断言为 Spec 新语义 |

## Open Questions
- 无