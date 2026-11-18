# Spec: Issue #827 — 跨流水线移交后需求归属 user_id 被清空，「我的需求」不可见

## Objective
修复跨流水线/场景移交链路清空需求卡片归属 user_id 的缺陷，让「我的需求」在归属异常时兜底可见，并回填存量 user_id=0 卡片。

## Root Cause
1. `ensure_issue_task`（issues_center.py:356-357）：assignee 变更时同时覆写 `user_id`，新 assignee 未注册平台 → user_id=0，清空原归属。
2. 「我的需求」mine 查询（issues.py:1940）：仅 `user_id=?` 严格匹配，user_id=0 后卡片对创建人不可见。
3. `issue.created_by` 列已存在（db_setup.py:5456 迁移加列）但 `ensure_issue_task` 从未写入 → 兜底无数据可用。

## Scope
### In Scope
1. 移交/assignee 变更链路保留 user_id：新 user_id=0 时不覆写已有非零值。
2. `ensure_issue_task` 写入 `created_by`（创建人 GitCode 用户名），复用时回补空值。
3. 「我的需求」过滤口径扩展为 `user_id=X OR created_by=X OR assignee=X`（绑定集合兜底）。
4. 回填脚本增强：`assignee` 不可映射时回退 `created_by` 解析。

### Out of Scope
- #705（assignee 维度清空）、#503（A2A user scope 丢失）的独立修复。
- 需求中心 UI 改版/列表排序。

## Acceptance Criteria
- [ ] assignee 变更但新 assignee 未注册时，已有 user_id 不被清零。
- [ ] `ensure_issue_task` 建卡/复用时写入 `created_by`（GitCode 用户名）。
- [ ] 「我的需求」在 user_id=0 但 created_by/assignee 命中当前用户时可见。
- [ ] 回填脚本支持 created_by 回退源。
- [ ] 新增测试覆盖以上场景；现有测试通过。
