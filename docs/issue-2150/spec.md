# Spec: Issue #2150 组织上下文与知识库基础能力

## Objective

为组织提供可管理的「组织上下文（brief/policy/decision）」与「组织知识库」，支持
状态流转（context: draft/active/deprecated；knowledge: draft/active/deprecated/archived）、权限分级、审计留痕与发布前敏感信息扫描，并为
下一单「下派注入」提供按组织隔离的稳定读取接口。

## Commands

```bash
python3 -m pytest tests/mgmt/test_org_context.py -q
python3 -m pytest tests/atomic/test_migration_track_guard.py -q
```

## Project Structure

- `src/api/db_setup.py`: `org_context` 建表迁移 + `agent_shared_knowledge` 扩展列迁移（注册式新轨）。
- `src/domains/mgmt/org_context_store.py`: 组织上下文领域逻辑 + 敏感扫描工具。
- `src/domains/mgmt/org_knowledge.py`: 组织知识领域逻辑（复用 `agent_shared_knowledge`）。
- `src/domains/mgmt/routes/org_context_store.py` / `routes/org_knowledge.py`: HTTP 路由。
- `src/api/grape_server.py` / `src/api/main.py`: `include_router` 注册（多进程 + 单进程两处）。
- `src/domains/mgmt/auth/audit.py`: `EVENT_TYPES` 登记 `org_context_*` / `org_knowledge_*`。
- `tests/mgmt/test_org_context.py`: 端到端契约测试。

## Contract

### 数据模型

- `org_context`（新表）：`org_id / kind / ctx_key / title / content / lifecycle_status /
  version / owner_id / valid_until / created_by / updated_by / created_at / updated_at`。
  - 正文直接落 DB 列 `content`（上下文片段，非知识库正文）。
  - `kind ∈ {brief, policy, decision}`；`lifecycle_status ∈ {draft, active, deprecated}`。
  - 每 org 每 kind 仅 1 条 active（`brief` 强制单 key；`policy/decision` 同 `ctx_key` 单 active）。
- `agent_shared_knowledge`（扩展列，不改既有 `status`/`archived`）：`lifecycle_status /
  org_id / kind / tags / applies_to / valid_until / review_interval_days / version /
  last_reviewed_at / owner_id`。
  - 正文沿用 `file_path`/`obs_key` 索引（正文落工作区文件/OBS，DB 不存正文），
    `repo_key` = 组织仓库。

### API

| 方法 | 路径 | 权限 |
|------|------|------|
| GET | `/api/orgs/{id}/context` | 组织成员 |
| POST | `/api/orgs/{id}/context` | owner/admin 或组织仓库 repo admin |
| GET | `/api/orgs/{id}/context/{cid}` | 组织成员 |
| PUT | `/api/orgs/{id}/context/{cid}` | owner/admin 或 repo admin |
| POST | `/api/orgs/{id}/context/{cid}/publish` | owner/admin |
| POST | `/api/orgs/{id}/context/{cid}/deprecate` | owner/admin |
| DELETE | `/api/orgs/{id}/context/{cid}` | owner/admin 或 repo admin |
| GET | `/api/orgs/{id}/knowledge` | 组织成员（支持 `kind/tag/q/status` 过滤） |
| POST | `/api/orgs/{id}/knowledge` | owner/admin 或 repo admin |
| GET | `/api/orgs/{id}/knowledge/{kid}` | 组织成员 |
| PUT | `/api/orgs/{id}/knowledge/{kid}` | owner/admin 或 repo admin |
| POST | `/api/orgs/{id}/knowledge/{kid}/publish` | owner/admin |
| POST | `/api/orgs/{id}/knowledge/{kid}/deprecate` | owner/admin |
| POST | `/api/orgs/{id}/knowledge/{kid}/archive` | owner/admin |
| DELETE | `/api/orgs/{id}/knowledge/{kid}` | owner/admin 或 repo admin |

### 状态与安全

- 发布：串行化锁（MySQL `GET_LOCK`，SQLite `db_lock`）内把同 `(org, kind, key)` 的旧
  active 自动降级为 deprecated，非 409；deprecated/archived 条目再次发布 →
  `409 INVALID_STATUS_TRANSITION`。
- 知识状态：`draft/active/deprecated/archived`；`archived` 终态，提供
  `POST /api/orgs/{id}/knowledge/{kid}/archive`（owner/admin）。
- 正文上限：复用 `routes/knowledge.py` 的 `MAX_CONTENT_BYTES`（默认 5MB、下限 1KB），
  超限返回 `400 CONTENT_TOO_LARGE`；name 复用 `MAX_NAME_LEN`（50），超限
  `400 INVALID_NAME`。
- 敏感扫描：发布前扫描正文 + 展示字段（context title；knowledge name/description）
  的明显凭据模式，命中返回 `400 SENSITIVE_CONTENT_REJECTED`。
- 旧知识 API 隔离：`scope='org'` 行不出现在旧 `/api/knowledge` 列表/统计，详情/
  更新/删除/关联端点按 404 处理（组织知识仅走 `/api/orgs/{id}/knowledge`）。
- 权限拒绝统一 `403 PERMISSION_DENIED`；非成员读 `403`；跨组织不可见 `404`。
- 非法 context kind 返回 `400 INVALID_CONTEXT_KIND`（对齐 Issue 契约）。
- 审计：创建/更新/发布/废弃/归档落 `audit_logs`（`org_context_*` / `org_knowledge_*`）。

## Testing Strategy

pytest + FastAPI `TestClient` + 真实 SQLite 测试库；`dependency_overrides` 注入用户身份。
测试 seam 为 HTTP 端点（用户可见行为）：发布自动降级、过滤、权限口径、内容上限、
凭据拒绝、审计落库、组织隔离。

## Boundaries

- Always: 注册式迁移、幂等、失败抛出；不改既有 `agent_shared_knowledge` 列语义。
- Ask first: 改动既有知识看板/注入读取口径，或引入跨组织共享。
