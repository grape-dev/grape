# Plan: Issue #2150 组织上下文与知识库

## 决策摘要

| 决策 | 选择 |
|------|------|
| 知识正文权威源 | 沿用 `file_path`/`obs_key` 索引，`repo_key` = 组织仓库；DB 不存正文 |
| 状态流转 | 新增 `lifecycle_status`，不改既有 `status`(accepted/rejected)/`archived` |
| 每 kind 1 条 active | 发布前 GET_LOCK 命名锁串行化 + 锁内复查后降级旧 active，非 DB 约束（MySQL 无部分唯一索引；FOR UPDATE 被只读自动 commit 释放） |
| 知识状态 | `draft/active/deprecated/archived`；deprecated/archived 为终态（不可再发布/更新） |
| 发布冲突语义 | 自动降级，契约无 `409 CONTEXT_ALREADY_ACTIVE` |
| 写 draft 口径 | org owner/admin + 组织仓库 repo admin |
| 模块命名 | `org_context_store.py` / `org_knowledge.py`（与 `src/api/org_context.py` 区分） |
| 迁移轨道 | `register_migration` 新轨，版本 `202611120004` / `202611120005`，幂等且失败抛出 |
| 路由注册 | `src/api/grape_server.py` include_router |
| 审计 | `EVENT_TYPES` frozenset 显式登记 |

## 架构

```
routes/org_context_store.py ─┐
                             ├─> org_repos.py (_error/_require_* 复用)
routes/org_knowledge.py ─────┘
        │                        │
        ▼                        ▼
org_context_store.py        org_knowledge.py
 (org_context 表 + 敏感扫描)   (agent_shared_knowledge 扩展)
        │                        │
        └──────► audit.log_audit ◄──────┘
```

## 数据层

- `_ORG_CONTEXT_DDL`: `id/org_id/kind/ctx_key/title/content/lifecycle_status/version/
  owner_id/valid_until/created_by/updated_by/created_at/updated_at`，索引
  `idx_org_context_org(org_id, kind, lifecycle_status)`。
- `agent_shared_knowledge` 扩展列由 `_ensure_column(..., strict=True)` 幂等补齐。
- `audit.py` `EVENT_TYPES` 增补 8 类事件。

## 发布降级（跨进程串行化）

PR #1278 审查修正：`SELECT ... FOR UPDATE` 在本仓库**不可用**——`mysql_compat`
对只读语句执行后自动 commit（`src/kernel/mysql_compat.py`），事务内行锁会被立即
释放，多进程并发发布仍可各自成功 → 同 key 多条 active。

改为 MySQL 命名锁 `GET_LOCK`（连接级，不受只读自动 commit 影响），锁的获取/释放
在同一条池外专用连接（`dedicated_connection`）上，连接断开即释放（异常路径不泄漏）；
SQLite 为单进程 + `db_lock` 串行，直接放行。

```
GET_LOCK('grape_org_pub:{table}:{org_id}:{sha1(group)}', 10)
  re-check lifecycle_status（deprecated/archived → 409）
  扫描 title/name/description/content 敏感内容
  UPDATE 同组旧 active → deprecated
  UPDATE target → active
RELEASE_LOCK + close
```

## 敏感扫描

`scan_sensitive_content(text) -> list[str]`：明显凭据模式（私钥块、云 AK、
`ghp_/sk-/xoxb-` 等 token、`password=/secret=` 赋值）。发布前对正文 + 展示字段
（context title；knowledge name/description）扫描，命中即拒绝，事件 detail 只记
模式名不记正文。

## 状态机与隔离

- 知识 `lifecycle_status ∈ {draft, active, deprecated, archived}`；`archived` 为终态
  （不可发布/更新/废弃），`deprecated` 不可再发布（409 INVALID_STATUS_TRANSITION）。
- 组织知识复用 `agent_shared_knowledge` 后，旧 `/api/knowledge` 列表/详情/更新/
  删除/关联端点一律排除 `scope='org'` 行（详情等按 404 处理），防止跨组织读取与
  生命周期绕过；组织知识只能走 `/api/orgs/{id}/knowledge`。

## 里程碑

- M1 数据层 + 审计：两笔迁移 + `EVENT_TYPES` 登记。
- M2 API 层：`org_context` / `org_knowledge` 全部端点 + 路由注册。
- M3 权限 + 安全 + 测试：权限分级、敏感扫描、`tests/mgmt/test_org_context.py` 全覆盖。
