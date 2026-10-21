# Spec: Issue #393 — 仓库的 admin 成员支持确认需求完成

> Coding Agent | 基于 Issue Agent 发布的 Spec（Issue #393 评论）| 2026-10-20 | 目标分支 `weekly-08-lyz`

## 1. 需求背景

需求流转到终态（QA 通过 / PR 合入）后，缺少由**仓库 admin 角色成员**发起的「确认需求完成」人工终审动作；
现有 `POST /api/issues/{issue_id}/confirm-done` 只做**归属校验**（`user_id=? OR user_id=0`），任何需求创建人即可确认，
没有仓库角色维度的权限校验，也无审计留痕。

## 2. 目标 / 范围

### In Scope
| # | 能力 | 说明 |
|---|------|------|
| F1 | 服务端角色校验 | 以**仓库真实角色**为准（GitCode collaborator permission：access_level≥50 或 permission=admin 或 owner；参考 #377 不信任本地缓存角色），GitHub 走 `get_github_collaborator_permission`（admin） |
| F2 | 确认 API | `POST /api/issues/{issue_id}/admin-confirm-done`：校验角色 → 需求 status=completed、stage=done → 关联 agent_tasks/流水线事件终态同步 → 关 VCS Issue（复用 `_close_vcs_issues` 降级策略） |
| F3 | 非 admin 拒绝 | 403 + 明确文案；前端入口置灰 + 提示权限不足 |
| F4 | 幂等 | 已 completed 再确认返回 `already_done:true`，无副作用；cancelled 400 |
| F5 | 审计留痕 | `src/auth/audit.py` 新增 `issue_admin_confirmed` 事件：操作人（operator_id）、时间（created_at）、目标需求（repo_key/issue_number/issue_id）|
| F6 | 前端入口 | 需求详情页 `_renderTaskActionsInline` 增加「管理员确认完成」按钮，按 repo admin 角色渲染可用/置灰 |

### Out of Scope
- 修改既有 `/confirm-done`（开发者本人确认）行为——两条通道并存。
- QA 合并权限（#386）、角色委托、历史数据批量补确认。

## 3. 方案设计

### 3.1 角色判定（决策：平台真实角色为准，兼容 #377）

```
GET /api/v5/repos/{owner}/{repo}/collaborators/{username}/permission
  → access_level ≥ 50 (Admin) 或 permission == "admin" → repo admin
  → owner 本人 = repo admin
```

实现位置：`src/auth/repo_roles.py`（新文件，auth 域角色校验 helper）
- `get_user_repo_admin(user, repo_key) -> dict | None`：返回 `{username, source}`；非 admin 返回 None。
- 取 username：`grape_accounts → external_identities (provider=gitcode, status=1).external_login`。
- 调用 GitCode API 时走 `with_oauth_token` 包装（预刷新 + 401 重试）；用户未绑定 gitcode → 非 admin（None）。
- GitHub 仓库走 `get_github_collaborator_permission(role_name == "admin")`。
- 查询失败（网络/403/404）：**fail-closed** 返回 None（宁可拒绝也不越权放行）。

### 3.2 API 设计（幂等）

`POST /api/issues/{issue_id}/admin-confirm-done`（user-only：`require_no_session_token`）
1. 404：需求不存在。
2. 403：`check_repo_access` 失败（非本仓库成员）或非 repo admin（文案「需要仓库管理员权限」）。
3. 400：cancelled。
4. 幂等：completed → `{ok, already_done:true}`，不重复关 VCS、不重复审计。
5. 正常路径（事务内）：`issue.status='completed'`、`completed_reason='admin_confirmed'`、`completed_at=now`；`agent_tasks` 未终态任务置 cancelled（终止流水线）；`set_issue_stage(repo_key, #num, 'done', 'admin')`；`events` 该需求 running 事件置 completed。
6. 审计：`log_audit("issue_admin_confirmed", operator_id=user.id, detail={repo_key, issue_id, issue_number, username})`。
7. VCS Issue 关闭复用 `_close_vcs_issues`（降级不阻断，与现有 confirm-done 一致）。

### 3.3 前端设计

- `_renderTaskActionsInline` 增加按钮：`status not in (completed, cancelled)` 时渲染。
- 角色数据源：`localStorage.repo_member_cache`（`/repos/member` 响应，`role === 'Admin'` / `can_host`），当前仓库 repo_key 匹配。
  - admin → 可点按钮 → `_grapeConfirm` 二次确认 → `POST /issues/{id}/admin-confirm-done`。
  - 非 admin → 置灰（disabled）+ title 提示「需要仓库管理员权限」。
- 服务端 403 为最终权威；前端置灰只是体验层。
- i18n：`locales/{zh,en}/tasks.json` 新增 `admin_confirm_done` / `admin_confirm_done_message` / `admin_only_hint`。

## 4. 影响模块

| 模块 | 改动 |
|------|------|
| `src/auth/repo_roles.py` | 新增（角色校验 helper） |
| `src/auth/audit.py` | EVENT_TYPES + `issue_admin_confirmed` |
| `src/api/routes/issues.py` | 新增端点 + agent_tasks/events 终态同步 |
| `src/api/static/js/pages/issues.js` | admin 入口渲染 + 置灰 |
| `locales/{zh,en}/tasks.json` | 文案 |
| `tests/test_issue_393_admin_confirm.py` | 新增测试 |

## 5. 验收标准（对照 Issue Spec Acceptance）

- [ ] admin 确认后：issue.status=completed、stage=done、agent_tasks 无未终态、events 无 running、VCS Issue 尝试关闭
- [ ] 非 admin → 403；未绑定/查询失败 → 403（fail-closed）
- [ ] audit_logs 含 `issue_admin_confirmed`（操作人/时间/目标需求）
- [ ] 重复确认幂等（already_done，无二次审计/二次关 VCS）
- [ ] 现有测试全过 + 新增测试覆盖以上场景

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| GitCode permission API 限流 | `get_member_permission` 已带退避重试；确认动作低频 |
| 角色查询失败误放行 | fail-closed：任何异常 → 403 |
| 与既有 confirm-done 并发 | 同一 db_lock 串行；先到者置 completed，后到者幂等返回 |
