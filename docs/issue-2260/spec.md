# Spec: Issue #2260 组织审计查询接口

> 目标分支（开发基线）：`arch-optimization-chenyan`

## Objective

新增只读接口 `GET /api/orgs/{id}/audit`，让组织 owner/admin 按事件类型 / 时间分页检索本组织
审计记录（组织供给、关联仓库、跨仓下派、组织上下文/知识、手动绑定等），敏感信息不外泄，
既有审计写入（哈希链）不受影响。

- 用户：组织 owner/admin。
- 成功标准：owner/admin 200 且只能看到本组织记录；member / 非成员 403；组织不存在 404；
  事件类型精确/前缀过滤有效；`detail` 脱敏；支持 `since` / `limit` / `offset`。

## Scope

### In Scope

- 新增 `src/domains/mgmt/routes/org_audit.py`（`APIRouter(prefix="/api/orgs")`，`GET /{org_id}/audit`）。
- `src/domains/mgmt/auth/audit.py` 新增只读 helper `query_org_audit()` / `mask_detail()`，不改 `log_audit`。
- 注册 `org_audit.router` 到 `src/api/main.py` 与 `src/api/grape_server.py` 两个入口。
- 新增 `tests/mgmt/test_org_audit.py`。

### Out of Scope

- 不改审计写入链路 / 哈希链、不改 `audit_logs` 表结构与写入格式。
- 不做前端页面、导出、全文搜索 UI、跨组织聚合查询。

## API Contract

`GET /api/orgs/{org_id}/audit?event_type=&limit=50&offset=0&since=`

- 成功 200：`{"items":[{"id","seq","event_type","operator_id","user_id","ip","detail","created_at"}],"total","limit","offset"}`
  - `items` 按 `id` 倒序；`total` = 匹配总数（与分页一致）；`limit`/`offset` 返回夹取后的实际值。
- 组织不存在 → 404 `{"code":404,"error_code":"ORG_NOT_FOUND","message":"组织不存在"}`。
- 非成员 / 非 owner-admin → 403 `{"code":403,"error_code":"PERMISSION_DENIED"}`。
- `tk_` 会话凭据 → 403（`require_no_session_token`）。

## 技术要点（含 4 项已核实偏差）

1. **路径型鉴权**：显式 `get_org` → 404、`get_member_role` → 403；**不**用上下文型 `require_org_admin`
   （#381/#395 已因其可越权弃用）。
2. **事件白名单以 `EVENT_TYPES` 为唯一来源**，禁止把用户输入拼进 LIKE：
   - 空 → 组织相关事件全集；非空先精确命中，否则在 `EVENT_TYPES` 内做前缀展开。
   - 别名：`org_dispatch_` → `{dispatch_created,target_created,dispatch_completed,dispatch_partial_failed,dispatch_cancelled,target_retried}`
     （实际事件名无 `org_dispatch_` 前缀）；`org_provision_` 当前无审计写入 → 空页。
   - 未知前缀 → 200 空列表。
3. **组织归属在 Python 解析 `detail`**（JSON 文本，双后端无统一 JSON 函数）：匹配 `org_id`，
   并纳入 `source_org_id` / `target_org_id`（`org_repo_transfer` 只写 source/target，不纳入则迁移不可见）。
4. **分页**：`limit` 夹取 `1..200`（默认 50）、`offset` 夹取 `>=0`；`total` 为全量匹配数，
   记录先切片、后脱敏（仅当前窗口做 `mask_detail`，行按固定块流式读取）。

### 脱敏（`mask_detail`，递归，深度 ≤3，超限 fail-closed）

- 键名先做归一化分词（camelCase / 连字符 / 下划线），再按 token 精确 + 连写后缀匹配：
  `token`/`secret`/`password`/`passwd`/`credential`/`authorization`/`cookie`、`*_token`/`*_secret`/
  `apiKey`/`X-Auth-Token` 等 → `"[REDACTED]"`；`repo_key`/`context_id` 等业务键不误伤。
- 正文键 `content/body/text/markdown/prompt` → 直接剔除（按分词 token 精确匹配，避免 `context` 被误判）。
- 递归深度超过 3 层的子树整棵丢弃为 `"[REDACTED]"`（fail-closed，不原样返回）。
- `ip` 按审计需要原样返回。

## Acceptance Criteria

- [ ] owner/admin 200；member / 非成员 403 `PERMISSION_DENIED`；组织不存在 404 `ORG_NOT_FOUND`
- [ ] 仅返回本组织记录；`org_repo_transfer` 按 source/target 正确归属；跨组织不可见
- [ ] `event_type` 精确/前缀过滤有效（含 `org_repo_`/`org_context_`/`org_knowledge_`/`org_dispatch_` 别名）；未知前缀 200 空列表
- [ ] `limit` 默认 50、最大 200，`offset` 生效，`id` 倒序，`total` 与分页一致
- [ ] `detail` 不含 token/secret/正文键；响应字段齐全
- [ ] `since` 有效过滤
- [ ] 现有测试全部通过；新增 `tests/mgmt/test_org_audit.py` 覆盖以上场景

## Open Questions

无。`org_provision_` 当前无审计写入，按空结果处理；若需补写审计事件，另开 Issue。
