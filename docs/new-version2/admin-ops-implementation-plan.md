# Grape 管理运营系统实施计划

> 版本：v1.0
> 日期：2026-08-08
> 前置：`admin-ops-design.md`（主设计）
> 说明：本文只定义**做什么、按什么顺序做、每步如何验收与回滚**。用例验收细则见 `admin-ops-acceptance-plan.md`。

---

## 一、升级原则

1. **业务不回归优先**：管理面全部为新增端点/表，不改既有业务 API 行为。以 `python -m pytest tests/ -v` 全绿 + 对应里程碑验收用例为门禁。
2. **默认兼容存量**：`role` 默认 `user`、`status` 默认 `active`、技能 `audit_status` 默认 `approved`——存量数据零改动。
3. **DB 幂等迁移**：所有 `ALTER TABLE ADD COLUMN` / 建表 / 种子导入均幂等（`PRAGMA table_info` 前置检查），迁移前备份 `data/sessions.db`。
4. **编码约束**：所有文件读写 `encoding='utf-8'`；严禁用 PowerShell 写含中文文件（`DEPLOYMENT-REMOTE.md` §6.1 教训）。
5. **逐级灰度**：A0 先落鉴权骨架，A1 才开放管理写操作；任一环节异常可回退到上一里程碑。

---

## 二、里程碑总览

| 阶段 | 主题 | 形态 | 结束门禁（DoD） |
|---|---|---|---|
| **A0** | RBAC 基建 | 新增表/依赖，不开放管理端点 | 单测绿；存量用户默认 user 不回归 |
| **A1** | 用户管理 + 白名单 + 审计 | 管理端点开放（users/whitelist） | 用户 CRUD/禁用/重置密码 + 白名单开关 E2E |
| **A2** | 运营看板（只读） | overview/repos/events/agents/im 聚合 | 管理台只读数据可见、统计正确 |
| **A3** | 治理能力 | 技能审核 / 公告 / 系统设置 / 审计导出 | 管理台全流程 E2E |
| **A4** | 前端 Admin SPA | 导航 + 页面 + 确认弹窗 | Playwright UI + 手工验收 |

---

## 三、里程碑任务清单

### A0 RBAC 基建

**建表（`db_setup.py` 新增幂等区，先备份 `data/sessions.db`）**
- [ ] `admin_roles`（种子：super_admin / ops_admin / repo_admin / auditor，permissions_json 见设计 §4.2）。
- [ ] `admin_role_assignments`（user_id + role + repo_scope，UNIQUE 三列）。
- [ ] `admin_audit_log`（含 action/target/ip/created_at 索引）。
- [ ] `admin_notices`、`login_whitelist`。

**`user_accounts` 幂等加列（`user/db.py` `init_db()` 迁移区）**
- [ ] `role TEXT DEFAULT 'user'`、`status TEXT DEFAULT 'active'`、`quota_json TEXT DEFAULT '{}'`、`last_login_at TEXT DEFAULT ''`、`remark TEXT DEFAULT ''`。

**角色种子（`db_setup.py` 或 `user/db.py`）**
- [ ] 内置角色不可删（`is_builtin=1`）；权限点集合以 JSON 存储。

**鉴权依赖（新文件 `src/api/deps_admin.py`）**
- [ ] `get_admin_user`：Bearer token → `validate_token` → 校验 `status=active` → 汇总角色权限。
- [ ] `require_permission(perm)` 依赖工厂：权限点校验，未授权 403。
- [ ] `repo_scope_check(repo_key)`：repo_admin 校验归属。

**禁用拦截（小改动，A0 一并落地）**
- [ ] `user/db.py:validate_token` 增加 `status='disabled'` 拦截。
- [ ] `user/db.py:verify_user` 增加 `status='disabled'` 拦截。
- [ ] `auth.py:gitcode_oauth_callback` 对 disabled 用户返回 403（OAuth 已存在用户分支）。

> 门禁：`test_admin_auth.py` 单测绿；既有 `test_login_whitelist.py` / `test_api_routes.py` 全绿（默认 user 角色回归）。

---

### A1 用户管理 + 白名单 + 审计

**服务层（`src/admin/service.py`）**
- [ ] `list_users` / `get_user_detail` / `create_user` / `set_user_status` / `reset_password` / `set_user_role`。
- [ ] 创建/禁用/重置密码均写 `admin_audit_log`（detail 脱敏）。

**管理端点（新文件 `src/api/routes/admin/users.py` + `whitelist.py`）**
- [ ] `GET/POST /api/admin/users`、`GET /api/admin/users/{id}`、`PUT .../status`、`PUT .../password`、`PUT .../role`。
- [ ] `GET/POST /api/admin/whitelist`、`DELETE /api/admin/whitelist/{login}`、`PUT /api/admin/whitelist/toggle`。

**白名单落库（`config.py` 改造）**
- [ ] `is_gitcode_user_allowed`：DB 优先（`login_whitelist`），JSON 兜底；`meta.whitelist_enabled` 开关（默认 1，`0`=全部放行）。
- [ ] 首次启动 JSON → DB 种子导入（幂等）。

**审计框架（`src/admin/audit.py`）**
- [ ] `log_audit(actor, action, target_type, target_id, detail, ip)` 工具。
- [ ] 管理写端点统一调用；detail 敏感字段 `[REDACTED]`。

> 门禁：`test_admin_users.py` / `test_admin_whitelist.py` E2E 绿；禁用用户即时失效、正常用户不回归。

---

### A2 运营看板（只读）

**聚合服务（`src/admin/service.py` 扩展）**
- [ ] `overview`：健康卡（`/health` + DB 自检）、用户/仓库/事件/Runtime/IM 统计、待办告警。
- [ ] `list_repos`（跨用户全量）、`repo_detail`。
- [ ] `list_events`（跨仓库分页/筛选）、`event_stats`（按天/Agent/类型聚合）、`event_detail`。
- [ ] `list_runtimes`（`backend_agents` 只读 + healthy）、`list_agents`（按仓库×Agent 配置）。
- [ ] `list_im_bots`（`global_im_bot_config` + BotManager 实时状态聚合）。

**管理端点（`admin/overview.py` / `repos.py` / `events.py` / `agents.py` / `im.py`）**
- [ ] 对应 §七 API 表中的只读端点。

> 门禁：`test_admin_overview.py` / `test_admin_repos.py` / `test_admin_events.py` 统计正确；只读端点无副作用。

---

### A3 治理能力

**技能审核（`admin/skills.py` + `skill` 表加列）**
- [ ] `ALTER TABLE skill ADD COLUMN audit_status TEXT DEFAULT 'approved'`（幂等）。
- [ ] 用户上传技能 `audit_status='pending'`（`skills.py` 上传路径小改）。
- [ ] `GET/POST /api/admin/skills`、`POST /api/admin/skills/sync-official`、`PUT /api/admin/skills/{id}/audit`。

**公告（`admin/notices.py`）**
- [ ] `GET/POST /api/admin/notices`、`PUT /api/admin/notices/{id}`。
- [ ] 业务面 `GET /api/notices/active`（公开只读 active）。

**系统设置（`admin/settings.py`）**
- [ ] `GET/PUT /api/admin/settings`：`meta.webhook_url` / `meta.execution_mode` / `meta.whitelist_enabled` / OAuth（仅 super_admin）。
- [ ] 写操作审计留痕。

**审计检索与导出（`admin/audit.py` 扩展）**
- [ ] `GET /api/admin/audit`（分页/筛选）。
- [ ] `GET /api/admin/audit/export`（CSV）。

> 门禁：`test_admin_skills.py` / `test_admin_notices.py` / `test_admin_settings.py` / `test_admin_audit.py` 全绿。

---

### A4 前端 Admin SPA

- [ ] `index.html` 增加 `Admin` 侧栏分组（仅拥有管理权限的用户可见；无权限隐藏）。
- [ ] `app.js` VIEWS 增加 `'admin'`，`admin?tab=xxx` 子路由。
- [ ] `js/pages/admin/{overview,users,whitelist,repos,agents,skills,events,im,notices,audit,settings}.js`。
- [ ] 高风险操作统一「确认弹窗 + 二次输入确认」。
- [ ] 公告 banner：登录页 / SPA 顶部拉取 `GET /api/notices/active`。
- [ ] `?v=` 缓存版本号提升（`index.html:8` / script 标签）。

> 门禁：Playwright UI 用例 + 手工验收。

---

## 四、部署与切换顺序

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 0 | 备份 `data/sessions.db` + 记录当前 commit | — | 恢复备份 / `git checkout` |
| 1 | 发布 **A0**（RBAC 基建，无管理端点开放） | 全量 pytest 绿 + 存量用户正常登录 | 无行为差异，可整体回滚 |
| 2 | 发布 **A1**（用户管理 + 白名单落库） | users/whitelist E2E；禁用即时生效 | 白名单逻辑回退 JSON；管理端点独立回滚 |
| 3 | 发布 **A2**（只读看板） | 统计正确 + 无副作用 | 纯新增，无回滚压力 |
| 4 | 发布 **A3**（治理能力） | 技能审核/公告/设置/审计全流程 | 审核位默认 approved 无阻塞 |
| 5 | 发布 **A4**（前端） | Playwright + 手工 | 前端独立回滚（`?v=` 恢复） |

> 每步灰度为可逆操作；管理功能开关存 `meta`，运维只需改键值 + 重启。

---

## 五、工作拆分建议（Git 提交粒度）

1. `A0`：`feat(admin): RBAC 基建（角色表/鉴权依赖/用户禁用拦截/加列）` —— 独立可回滚。
2. `A1`：`feat(admin): 用户管理 + 白名单落库 + 审计框架`。
3. `A2`：`feat(admin): 运营看板只读端点（overview/repos/events/agents/im）`。
4. `A3`：`feat(admin): 技能审核/公告/系统设置/审计导出`。
5. `A4`：`feat(admin-ui): Admin SPA + 导航 + 确认弹窗 + 公告 banner`。

每个里程碑独立提交，便于按 §四 逐级发布与回滚。

---

## 六、风险与应对

| 风险 | 应对 |
|---|---|
| 存量用户无角色导致功能不可用 | 默认 `role='user'` + `admin_role_assignments` 空 = 无管理权限，业务面不受影响 |
| `validate_token` 加禁用拦截影响正常用户 | 仅 `status='disabled'` 拦截；回归用例覆盖正常/禁用两态 |
| 白名单落库后 JSON 不一致 | 种子导入幂等 + JSON 兜底；以 DB 为准并可在管理台查看 |
| 管理端点暴露面扩大 | RBAC 权限点 + 审计 + 前端隐藏入口；`settings.manage` 仅 super_admin |
| Runtime 主线（agent-runtime）演进并行 | 本设计只读聚合，不写运行时状态，两线无冲突 |
| 审核位引入上传阻塞 | `audit_status` 默认 approved，上传流程不变，审核为可选治理 |
