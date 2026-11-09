# Spec: Issue #491 — 管理员确认完成非本人创建需求报「需要仓库管理员权限」（回归）

> 来源：Issue #491 VOD/Issue Agent Spec + Coding Agent 代码级根因复核 | 2026-10-28

## 症状
管理员（如 deli007）在 Issue 详情点「管理员确认完成」，前端弹「操作失败: 需要仓库管理员权限：仅仓库 Owner/Admin 成员可确认需求完成」（后端 403）。需求由他人创建（跨用户场景）。

## 代码级根因（已在本地复现验证）
`POST /api/issues/{id}/admin-confirm-done`（src/api/routes/issues.py:721）本身**没有**「创建者==操作者」判断（#393 实现即支持跨归属确认）。真正根因是**两个 GitCode API 的 admin 口径不一致**，导致「前端认为是 Admin → 按钮可点」但「后端判非 Admin → 403」：

1. 前端数据源 `/repos/member` ← `fetch_repos_with_role`（/user/repos 列表）：`permission.admin=true` 即 `is_admin=true`（src/user/repo_manager.py:315）。
2. 后端终审判定 `repo_roles._check_gitcode_admin`（/repos/{owner}/{repo}/collaborators/{username}/permission 单查）：仅认 `access_level>=50` 或 `permission=="admin"` 或复数 `permissions.admin`（src/auth/repo_roles.py:72-77）。

GitCode **自定义管理员角色**（组织管理员/自定义 admin 角色成员）在单查端点返回 `role_name="Admin"` 但 `access_level=20`、`permission="custom"`、复数 `permissions.admin=false`——三条判定链全部落空 → fail-closed 返回 None → 403。

复现脚本（模拟两端口点返回）确认：列表口径 `is_admin=True`，collaborator 单查口径 `admin=False`，与线上症状完全一致。

> #393 测试全部 mock `_check_gitcode_admin`，未覆盖真实 GitCode 返回 `role_name=Admin` + `access_level<50` 的形态，故回归未被发现。

## 修复方案
对齐两端口径：在 `_check_gitcode_admin` 判定链**最前**加 `role_name` 判定——`role_name == "Admin"`（大小写不敏感）即 admin。

- role_name 是 GitCode 该端点返回的角色名字段（如 "Admin"/"Developer"/"Maintainer"/自定义角色名），自定义管理员角色的 role_name 为 "Admin"。
- 仅加分支不删原分支（access_level/permission/permissions 兜底保留），保持 fail-closed 不变。
- 与 Issue/Spec 的映射：Spec 所述「移除创建者==操作者隐含判断」经代码核实并不存在；真实缺陷是角色判定链缺 role_name 分支，属「admin/owner 角色识别未覆盖（自定义角色形态的）跨用户确认场景」，本修复达成同一目标：Admin/Owner 确认他人需求成功。
- 安全边界不变：非 admin 非 creator 仍 403（creator 本人走 #131 confirm-done 通道，本端点权限口径不放宽给非 admin）。

## 测试计划（TDD：先红后绿）
- 单元（新 tests/test_issue_491_repo_roles.py）：
  1. `role_name="Admin"` + `access_level=20` + `permission="custom"` → admin=True（红：修复前 False）
  2. `role_name="Admin"` + `access_level=30`（developer）→ admin=True（红线对齐）
  3. `role_name="Developer"` + `access_level=30` → admin=False（安全边界）
  4. `role_name="Maintainer"` + `access_level=40` → admin=False（#393 终审口径不破坏）
- 集成（扩 tests/test_issue_393_admin_confirm.py 同文件内新类，保持先例路径回归）：
  5. 管理员（role_name=Admin 自定义角色）确认**他人创建**需求 → 200 completed
  6. 非 admin 非 creator（role_name=Developer）→ 403（安全边界）
  7. 仓库 Owner 本人确认他人需求 → 200（owner 快速路径回归）
  8. #393 原有 15 用例全量回归通过

## 验收标准（对照 Issue Spec Acceptance Criteria）
- [x] Admin 角色成员（含自定义 admin 角色形态）确认非本人创建需求成功
- [x] Owner 确认非本人创建需求成功
- [x] 权限校验不再误报权限不足（两端口径对齐）
- [x] 非 admin 非 creator 仍被拒绝
- [x] 新增测试覆盖并全部通过
- [x] 现有测试不破坏（#393 15 用例 + 全量回归）
