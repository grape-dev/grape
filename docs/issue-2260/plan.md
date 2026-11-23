# Plan: Issue #2260 组织审计查询接口

## 决策摘要

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 事件过滤 | 直接 LIKE 前缀 / `EVENT_TYPES` 白名单展开 | 白名单展开 | 防注入，且避免 `org_dispatch_` 匹配不到实际事件 |
| 组织过滤 | SQL JSON 函数 / Python 解析 | Python 解析 | 双后端（SQLite/MySQL）无统一 JSON 支持 |
| 鉴权 | `require_org_admin` / 路径型显式校验 | 路径型显式校验 | 上下文型鉴权可越权（#381/#395） |
| 排序 | `seq` / `id` | `id` | 契约要求 id 倒序，id 与 seq 同步自增 |
| limit/offset 越界 | 422 / 夹取 | 夹取 | 契约要求返回夹取后的实际值 |

## 架构与数据流

```
GET /api/orgs/{org_id}/audit
  → org_audit.get_org_audit()             # 路径型鉴权（404 / 403）+ 参数透传
      → audit.query_org_audit(org_id, event_type, limit, offset, since)
          → _resolve_event_types(event_type)   # EVENT_TYPES 白名单 + 别名/前缀展开
          → SQL: SELECT ... FROM audit_logs WHERE event_type IN (...) [AND created_at>=?] ORDER BY id DESC
          → Python: 流式分块读取(fetchmany) → detail 解析 → 组织归属匹配(org_id/source_org_id/target_org_id)
          → total 全量计数；仅分页窗口内记录 mask_detail() 脱敏（先切片后脱敏，内存/CPU 有界）
```

## 模块改动

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/domains/mgmt/auth/audit.py` | 修改 | 新增 `ORG_AUDIT_EVENT_TYPES` / `_resolve_event_types` / `_normalize_since` / `_org_matches` / `mask_detail` / `query_org_audit`（不改 `log_audit`） |
| `src/domains/mgmt/routes/org_audit.py` | 新增 | Router + 路径型鉴权 + 契约错误码 |
| `src/api/main.py` | 修改 | `include_router(org_audit.router)` |
| `src/api/grape_server.py` | 修改 | `include_router(org_audit.router)` |
| `tests/mgmt/test_org_audit.py` | 新增 | 覆盖鉴权/过滤/分页/脱敏/隔离 |
| `docs/issue-2260/*.md` | 新增 | 需求/方案/任务拆解 |

依赖：`org_audit` → `auth.audit`（`EVENT_TYPES`、`db`、`db_lock`）；→ `user.org_db.get_member_role`；
→ `routes.org_repos._error`/`_require_org`（复用统一错误封装与 org 存在性校验）；
→ `api.deps.require_no_session_token`。

## 测试 seam

- 最高 seam：HTTP 接口（`TestClient` + JWT 头），断言用户可见行为（状态码 / 契约字段 / 隔离 / 脱敏）。
- 纯函数 seam：`mask_detail` / `query_org_audit` 直接调用，覆盖深层结构与边界夹取。
- 复用既有模式：`tests/runtime/test_issue_395_bugfixes.py`（真实测试库 + 建组织/成员 helper）。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `detail` JSON 无法索引，`total` 仍需扫描事件白名单候选行 | 中 | 行按块流式读取（不全量 fetchall）+ 先切片后脱敏（窗口外不脱敏）；org_id 派生列/索引另开 Issue #2334 跟进 |
| 前缀别名与实际事件名漂移 | 中 | 以 `EVENT_TYPES` 为唯一来源 + 别名映射，测试固定覆盖 |
| 脱敏遗漏 | 高 | 键名分词后按 token 精确/后缀匹配（覆盖 `*_token`/`*_secret`/camelCase/Header 式键）；深度超限 fail-closed 丢弃子树；测试断言原始 secret 不出现在响应中 |
