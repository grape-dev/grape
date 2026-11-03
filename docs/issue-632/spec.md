# Spec: git-repo milestones create/update 路由（Issue #632）

> 来源：Issue #632（用户反馈 #239）｜对齐 #577 Spec｜目标分支 weekly-08-mys｜2026-11-01

## Objective
服务端 `git_repo_api.py` 增加 `POST /{owner}/{repo}/milestones` 与 `PATCH /{owner}/{repo}/milestones/{number}` 路由，复用 provider 透传（`_vcs_req` 直调 VCS），契约刷新后 CLI 生成 `git-repo milestones create/update` 子命令，使 `issues update --milestone_id` 有真实落地路径。

## Scope
- In: POST/PATCH milestones 路由 + Body 模型（MilestoneCreateBody/MilestoneUpdateBody）；OpenAPI 契约（operation_id `gitrepo_milestones_create`/`gitrepo_milestones_update`）；测试覆盖；docs/openapi/10-git-repos.md §6 文档更新。
- Out: `labels create --json` 400（grape-dev/grape-cli#8 独立跟进）；`issues update --milestone_id` 本身（已支持）；grape-skills 仓的 grape-cli-reference.md / issue-runbook.md 降级说明（属 grape-skills 仓，本 PR 在评论中说明移交）。

## 技术方案
与现有 labels/issues 路由同构：RESTful + `_vcs_req` provider 透传（GitHub/GitCode milestones 字段 title/description/due_on/state 语义一致，无需 translate_inbound）。空值字段不透传（对齐 update_pull 的 payload 过滤模式）。

## Acceptance Criteria
- [ ] POST /milestones 透传 title/description/due_on，返回创建后里程碑对象（含 id）
- [ ] PATCH /milestones/{number} 透传 title/description/due_on/state
- [ ] 测试：test_git_repo_api.py 新增 POST/PATCH 断言（method/url/body）；test_openapi.py 新增 operation_id 映射
- [ ] docs/openapi/10-git-repos.md §6 补 POST/PATCH 说明
- [ ] 现有测试全通过
