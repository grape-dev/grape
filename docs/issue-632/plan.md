# Plan: git-repo milestones create/update 路由（Issue #632）

## 实现路径
1. `src/api/git_repo_api.py`：新增 `MilestoneCreateBody(title*, description, due_on)`、`MilestoneUpdateBody(title, description, due_on, state)`；在 `list_milestones` 后注册 `POST /{owner}/{repo}/milestones`（operation_id=gitrepo_milestones_create）与 `PATCH /{owner}/{repo}/milestones/{number}`（operation_id=gitrepo_milestones_update），均走 `_vcs_req` provider 透传，空值字段过滤。
2. 测试（TDD）：`tests/test_git_repo_api.py` 先加失败断言（RED）→ 实现路由（GREEN）；`tests/test_openapi.py` OPERATION 映射表补两个新 operation_id。
3. 文档：`docs/openapi/10-git-repos.md` §6 补 POST/PATCH 接口说明（入参表 + 返回 + 错误码）。

## 设计决策
- RESTful 路由 + 与 issues/labels 同构：零新概念，CLI 契约自动生成。
- 不走 translate_inbound：GitHub/GitCode milestones 字段语义一致（直调透传，与 labels 一致）。
- due_on 用 str（ISO 时间戳透传，不做日期解析，保持透传语义）。

## 风险
- #577 同根因已 pending_review：已核对无 PR/实现进展（pipeline 仅 vod+issue），无重复开发。
- 契约刷新（grape refresh）依赖服务端部署，本 PR 合入后生效。
