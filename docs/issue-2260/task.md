# Task Breakdown: Issue #2260 组织审计查询接口

> 每个竖切片先写失败用例（`test(...)` 提交），再落最小实现（`feat(...)` 提交）。

## Slice 1 — 鉴权 + 事件白名单/前缀 + 契约字段

- [ ] `tests/mgmt/test_org_audit.py`：owner/admin 200、member 403、非成员 403、org 不存在 404、
      `tk_` 403、响应字段齐全（红）
- [ ] `audit.py`：`ORG_AUDIT_EVENT_TYPES`（源自 `EVENT_TYPES`）+ `_resolve_event_types`
      （精确 → 别名 `org_dispatch_`/`org_provision_` → 前缀展开）
- [ ] `audit.py`：`query_org_audit`（白名单 IN 查询 + id 倒序 + total/切片）
- [ ] `org_audit.py`：Router + 路径型鉴权 + 契约错误码；两入口注册

## Slice 2 — 组织隔离 + 脱敏

- [ ] 测试：跨组织不可见、`org_repo_transfer` 按 source/target 归属、`detail` 递归脱敏、
      正文键剔除、原始 secret 不出现在响应（红）
- [ ] `audit.py`：`_org_matches`（org_id/source_org_id/target_org_id）+ `mask_detail`

## Slice 3 — 分页 / since / 边界

- [ ] 测试：id 倒序、`limit` 默认 50 / 最大 200 / 下限 1、`offset` 夹取、`total` 与分页一致、
      `since` 生效、未知前缀空列表（红）
- [ ] `audit.py`：`_normalize_since` + limit/offset 夹取

## Slice 5 — PR 审查修复（#1344 反馈）

- [x] 脱敏 fail-closed：深度超限丢弃子树；敏感键改分词 + 后缀匹配（覆盖 `*_token`/`*_secret`/camelCase/Header）
- [x] 查询有界：行流式分块读取 + 先切片后脱敏（窗口外不构造/不脱敏），`total` 全量计数
- [x] 用例接入 CI 门禁：`.github/workflows/windows-smoke.yml` 增 `tests/mgmt/test_org_audit.py` 步骤
- [x] 复用 `org_repos._error`/`_require_org`；补 `help/api/16-auth-and-misc.md` 端点文档与 `tests/api/test_openapi.py` 白名单
- [x] 修正 `org_provision_` 无数据时的组织供给表述（路由/查询 docstring、spec）
- [x] 登记后续 Issue #2334（`audit_logs.org_id` 派生列/索引，消除 detail 全量扫描）

## Slice 4 — 自审查 + PR + A2A QA

- [ ] 5 轴审查 + 开源前置增量检查（无关内容拦截 + 敏感信息增量扫描）
- [ ] 建 PR（base=`arch-optimization-chenyan`）+ Issue 评论告知
- [ ] A2A 通知 QA 审查（校验场景含 qa 节点）
