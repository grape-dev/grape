# Task Checklist: Issue #2150

- [x] Task 1: 数据层迁移 + 审计登记
  - Acceptance: `org_context` 表创建、`agent_shared_knowledge` 扩展列幂等补齐、事件白名单登记。
  - Verify: `python3 -m pytest tests/atomic/test_migration_track_guard.py -q` → 3 passed
  - Files: `src/api/db_setup.py`, `src/domains/mgmt/auth/audit.py`

- [x] Task 2: 领域逻辑
  - Acceptance: org_context/org_knowledge 的 CRUD + 状态流转 + 过滤查询 + 敏感扫描可用。
  - Verify: `python3 -m pytest tests/mgmt/test_org_context.py -q`
  - Files: `src/domains/mgmt/org_context_store.py`, `src/domains/mgmt/org_knowledge.py`

- [x] Task 3: HTTP 路由 + 注册
  - Acceptance: 端点按契约暴露，权限口径正确，路由已 include_router。
  - Verify: `python3 -m pytest tests/mgmt/test_org_context.py -q`
  - Files: `src/domains/mgmt/routes/org_context_store.py`, `routes/org_knowledge.py`,
    `src/api/grape_server.py`

- [x] Task 4: 端到端测试
  - Acceptance: 发布自动降级、kind/tag/q/status 过滤、403 口径、CONTENT_TOO_LARGE、
    凭据拒绝、审计落库、org 隔离全覆盖。
  - Verify: `python3 -m pytest tests/mgmt/test_org_context.py -q`
  - Files: `tests/mgmt/test_org_context.py`

- [x] Task 5: PR #1278 审查修复（1 Critical + 7 Required）
  - [x] 迁移 TEXT NOT NULL DEFAULT 列补 `allow_copy=True`（`202611120005`）
  - [x] 发布串行化：MySQL `GET_LOCK`（池外专用连接）+ SQLite `db_lock`，替代失效的 `FOR UPDATE`
  - [x] 旧 `/api/knowledge` 端点隔离 `scope='org'`（列表/统计隐藏，详情/更新/删除/关联 404）
  - [x] deprecated/archived 终态不可再发布/更新（409 INVALID_STATUS_TRANSITION）
  - [x] `INVALID_KIND` → `INVALID_CONTEXT_KIND`（对齐 Issue 契约）
  - [x] `tests/mgmt/test_org_context.py` 接入 linux-backend CI 门禁
  - [x] 知识补 `archived` 状态 + `/archive` 端点 + 审计事件
  - [x] Optional：name 限长（INVALID_NAME）、发布扫描展示字段、删除清理 OBS、正文读取移出 db_lock
  - Verify: `python3 -m pytest tests/mgmt/test_org_context.py -q` → 21 passed
  - Files: 上述领域/路由/迁移/CI 文件