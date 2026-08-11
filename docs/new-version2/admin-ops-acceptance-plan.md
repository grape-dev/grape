# Grape 管理运营系统用例验收计划

> 版本：v1.0
> 日期：2026-08-08
> 配套：`admin-ops-design.md`（主设计）/ `admin-ops-implementation-plan.md`（实施顺序）
> 说明：按里程碑 A0→A4 组织验收用例。所有里程碑需满足 §七「有序升级检查单」才能进入下一阶段。

---

## 一、验收分层定义

| 层级 | 代号 | 执行方式 | 工具 |
|---|---|---|---|
| 单元 | UT | 直接调用函数/类 | pytest（`DEVOPS_DB_PATH` 隔离） |
| 集成 | IT | 起 FastAPI `TestClient` 打 API | pytest + `tests/conftest.py` |
| 端到端 | E2E | 真实 server + 管理台操作 | pytest / 手工脚本 |
| UI | UI | 浏览器操作 | Playwright（`ui_*.py`） |
| 安全 | SEC | 认证/授权/脱敏 | pytest + 手工 |
| 迁移 | MIG | 旧库升级 / 旧数据兼容 | 脚本 + 手工 |

---

## 二、A0 RBAC 基建

> 验收标准：`python -m pytest tests/ -v` 全绿；存量用户默认 `role=user` 不回归。

| ID | 层级 | 场景 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|---|
| A0-01 | UT | 角色种子 | A0 完成 | 查 `admin_roles` | 存在 super_admin/ops_admin/repo_admin/auditor，`is_builtin=1`，permissions_json 完整 |
| A0-02 | UT | user_accounts 加列 | 旧库 | 跑 `init_db()` | `role/status/quota_json/last_login_at/remark` 列存在；存量行 `role='user'`、`status='active'`；重复执行幂等 |
| A0-03 | UT | 权限点校验 | 造 user+role | `require_permission('users.manage')` 对无权限用户 | 抛 403；有权限用户通过 |
| A0-04 | UT | repo_scope 校验 | repo_admin+scope | 访问 scope 内/外仓库 | 内通过、外 403 |
| A0-05 | UT | 禁用用户 token 拦截 | 用户 status=disabled | `validate_token(token)` | 返回 None（立即失效） |
| A0-06 | UT | 禁用用户密码拦截 | 用户 status=disabled | `verify_user(username, password)` | 返回 None |
| A0-07 | IT | 禁用用户 OAuth 拦截 | 已存在 disabled 用户 | `gitcode_oauth_callback` 命中该用户 | 403「账号已被禁用」 |
| A0-08 | MIG | 老库无新列 | 全新库 | 跑 `init_db()` | 不报错，按默认值建列 |
| A0-09 | UT | 回归 | A0 完成 | 跑 `test_login_whitelist.py` / `test_api_routes.py` | 全绿（默认 user 角色无影响） |

---

## 三、A1 用户管理 + 白名单 + 审计

> 验收标准：`test_admin_auth.py` / `test_admin_users.py` / `test_admin_whitelist.py` 全绿；禁用即时生效。

### 3.1 用户管理

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A1-U1 | IT | 用户列表 | 造多用户 | 分页返回 username/role/status；按角色/状态过滤正确 |
| A1-U2 | IT | 用户详情 | 造用户+凭据+仓库 | 返回基本信息+角色+配额+凭据存在性（无明文）+关联仓库/事件数 |
| A1-U3 | IT | 创建用户 | POST `/api/admin/users` | 创建成功；`role='user'` 默认；返回 id；审计有 `create_user` 记录 |
| A1-U4 | IT | 创建重复用户 | 已存在用户名 | 409 |
| A1-U5 | IT | 禁用用户 | PUT `/users/{id}/status` | `status='disabled'`；该用户 token/密码/OAuth 全部失效；审计记录 |
| A1-U6 | IT | 启用用户 | PUT `/users/{id}/status` | `status='active'`；重新可登录 |
| A1-U7 | IT | 重置密码 | PUT `/users/{id}/password` | 新密码可登录；旧密码失效；明文仅返回一次；审计 detail 无明文 |
| A1-U8 | IT | 修改角色 | PUT `/users/{id}/role` | 角色更新；含 repo_scope 时生效；审计记录 |

### 3.2 白名单

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A1-W1 | IT | 白名单列表 | 造数据 | 分页返回 gitcode_login + note |
| A1-W2 | IT | 新增白名单 | POST `/api/admin/whitelist` | 新增成功；审计记录；该 login 立即可登录 |
| A1-W3 | IT | 删除白名单 | DELETE `/whitelist/{login}` | 删除成功；该 login 下次登录被拒（403） |
| A1-W4 | IT | 全局开关 | PUT `/whitelist/toggle` → `whitelist_enabled=0` | 未在白名单用户也能登录（全部放行） |
| A1-W5 | UT | DB 优先/JSON 兜底 | DB 有 + JSON 有/无 | `is_gitcode_user_allowed` 以 DB 为准；DB 无记录回退 JSON |
| A1-W6 | MIG | JSON 种子导入 | 首次启动 | `login_whitelist` 从 `configs/login-whitelist.json` 导入，幂等不重复 |

### 3.3 审计

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A1-A1 | UT | 审计写留痕 | 任一管理写操作 | `admin_audit_log` 有记录：actor/action/target/ip/created_at |
| A1-A2 | UT | 敏感脱敏 | detail 含密码/token | detail 中替换为 `[REDACTED]` |
| A1-A3 | SEC | 无权限访问 | 普通 user token 调管理端点 | 403 |
| A1-A4 | SEC | 未登录访问 | 无 token 调管理端点 | 401 |

---

## 四、A2 运营看板（只读）

> 验收标准：`test_admin_overview.py` / `test_admin_repos.py` / `test_admin_events.py` 统计正确；只读无副作用。

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A2-O1 | IT | 总览 | GET `/api/admin/overview` | 健康卡/用户/仓库/事件/Runtime/IM 统计齐全且与 DB 一致 |
| A2-O2 | IT | 健康卡 | 正常环境 | 主服务 ok、webhook ok、DB 可写、IM WS 状态正确 |
| A2-O3 | IT | 仓库列表 | 造多托管仓库 | 跨用户全量；repo_key/托管人/webhook 状态/bot_username/dev_scene |
| A2-O4 | IT | 仓库详情 | 造仓库+事件+Agent | 关联 Agent 配置、事件数、技能、绑定 Bot |
| A2-O5 | IT | 事件列表 | 造多事件 | 跨仓库分页/状态筛选正确 |
| A2-O6 | IT | 事件统计 | 造多事件 | 按天/Agent/类型聚合与 SQL 一致 |
| A2-O7 | IT | 事件详情 | 造事件含 logs | 返回 payload/agent_logs/result（脱敏） |
| A2-O8 | IT | Runtime 列表 | 造 backend_agents | installed/healthy/version/cli_path 只读正确 |
| A2-O9 | IT | Agent 配置 | 造 devops_agent_session | 按仓库×Agent 展示 backend/skill/status |
| A2-O10 | IT | IM Bot 列表 | BotManager 起 1+ bot | 返回实时状态（connected/reconnecting/stopped/failed）+ 指标 |
| A2-O11 | SEC | 只读无副作用 | 调全部只读端点 | 数据无任何写入（对比前后 DB hash） |
| A2-O12 | SEC | 权限点 | repo_admin 调全局 overview | 403（无全局权限） |

---

## 五、A3 治理能力

> 验收标准：`test_admin_skills.py` / `test_admin_notices.py` / `test_admin_settings.py` / `test_admin_audit.py` 全绿。

### 5.1 技能审核

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A3-S1 | MIG | skill 表加列 | 旧库 | `audit_status` 列存在；存量行默认 `approved`；幂等 |
| A3-S2 | IT | 用户上传 pending | 用户上传技能 | 新技能 `audit_status='pending'`；业务面技能列表不展示 pending（或标注待审核） |
| A3-S3 | IT | 审核通过 | PUT `/admin/skills/{id}/audit` | 状态 `approved`；可被 agent 解析使用 |
| A3-S4 | IT | 审核驳回 | PUT 驳回 | 状态 `rejected`；业务面不可用 |
| A3-S5 | IT | 禁用技能 | 置 `is_active=0` | `resolve_skill_id`/`get_skill_by_id` 不再返回 |
| A3-S6 | IT | 官方同步 | POST `/admin/skills/sync-official` | 官方技能同步，审计记录 |
| A3-S7 | UT | 存量不回归 | 无 pending 场景 | 上传流程与审核前一致 |

### 5.2 公告

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A3-N1 | IT | 发布公告 | POST `/api/admin/notices` | 创建成功，is_active=1 |
| A3-N2 | IT | 业务面读取 | GET `/api/notices/active` | 仅返回 active 且未过期公告；无鉴权可访问 |
| A3-N3 | IT | 下线公告 | PUT 置 is_active=0 | 业务面不再返回 |
| A3-N4 | SEC | 业务面只读 | 无 token 调 `/api/admin/notices` | 401/403 |

### 5.3 系统设置

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A3-C1 | IT | 读设置 | GET `/api/admin/settings` | 返回 webhook_url/execution_mode/whitelist_enabled/OAuth 存在性（脱敏） |
| A3-C2 | IT | 改执行模式 | PUT `execution_mode=queue_local` | meta 表更新；写审计 |
| A3-C3 | SEC | 权限点 | ops_admin 改 OAuth | 403（`settings.manage` 仅 super_admin） |
| A3-C4 | SEC | 敏感脱敏 | 读设置 | 不返回 client_secret 明文 |

### 5.4 审计检索导出

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A3-A1 | IT | 审计列表 | 造审计数据 | 按 actor/action/时间/目标过滤分页正确 |
| A3-A2 | IT | CSV 导出 | GET `/api/admin/audit/export` | 返回 CSV，含头部；数据完整 |
| A3-A3 | SEC | 审计权限 | user 调 audit 端点 | 403 |

---

## 六、A4 前端 Admin SPA

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| A4-U1 | UI | 导航可见性 | admin 登录 vs 普通用户 | admin 看到 Admin 分组；普通用户不显示 |
| A4-U2 | UI | 总览页 | 打开 Admin → Overview | 各统计卡渲染；健康四态徽标正确 |
| A4-U3 | UI | 用户管理 | 列表/搜索/创建/禁用/重置 | 交互闭环；确认弹窗出现 |
| A4-U4 | UI | 高风险确认 | 解除托管/禁用用户/重置密码 | 二次确认弹窗；取消不执行 |
| A4-U5 | UI | 白名单页 | 增删改 + 开关 | 操作生效；审计可见 |
| A4-U6 | UI | 事件页 | 筛选/详情 | 跨仓库事件流；详情含日志 |
| A4-U7 | UI | IM Bot 页 | 查看状态 | 在线/离线徽标；单 Bot 指标展示 |
| A4-U8 | UI | 公告 banner | 有 active 公告 | 登录页/SPA 顶部展示；下线后消失 |
| A4-U9 | UI | 导航回归 | 全部侧栏入口 | 无 404；`?v=` 新版本号生效 |
| A4-U10 | SEC | 前端权限隐藏 | 无权限用户直接输 URL | 后端 403 兜底（前端隐藏仅为 UX） |

---

## 七、有序升级检查单（每阶段门禁）

> 任一未过即**不得进入下一阶段**。

| # | 检查项 | A0 | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|---|
| 1 | `python -m pytest tests/ -v` 全绿 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 2 | 新增测试文件存在且通过（`test_admin_*`） | — | ✅ | ✅ | ✅ | ✅ |
| 3 | 存量用户默认 `role=user` 不回归 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 4 | DB 迁移幂等 + 迁移前备份 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 5 | 对应里程碑 E2E 用例全部通过 | — | ✅ | ✅ | ✅ | — |
| 6 | 管理写操作全部审计留痕 | — | ✅ | ✅ | ✅ | ✅ |
| 7 | 管理端点 RBAC 权限点全覆盖 | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## 八、回归清单（每次发布前跑）

- `tests/test_api_routes.py`——auth/users 相关业务端点不回归。
- `tests/test_login_whitelist.py`——白名单逻辑改造后回归。
- `tests/test_session_manager.py` / `tests/test_chat_handler.py` / `tests/test_feishu_*.py`——IM/会话链路。
- `tests/test_agent_executor.py` / `tests/test_phase9.py`——Agent/Runtime 不回归。
- `tests/test_pipeline.py` / `tests/test_tasks.py` / `tests/test_onboarding.py`——业务链路。
- `ui_test_playwright.py` / `ui_scenario_test.py`——前端导航回归。

---

## 九、结论判定

1. **每个里程碑**：本阶段全部用例 + 回归清单通过 → 标记 DoD 完成，进入下一阶段。
2. **全部完成**：A0–A4 门禁全绿 + 部署按 `admin-ops-implementation-plan.md` §四 顺序灰度验证通过 → 管理运营系统收尾。
3. **任一回退**：回滚到上一里程碑 commit，记录异常并补充用例后再继续。
