# Spec: Issue #649 — 创建 Issue 时 assignee 不默认填创建者 + 成员资格校验 + 上游 403 非成员归类

> 来源：Issue #649 Spec（issue agent 已发布，本文件为开发侧落地摘要）| 目标分支：weekly-08-mys

## Objective
修复服务端创建 Issue 链路：
1. 不传 assignee 时不再默认填创建者（留空，由仓库方后续分派）；
2. 显式传 assignee 时在调用 GitCode 上游创建前校验仓库成员资格，非成员直接给出「非成员」语义错误（含 trace_id）；
3. 上游 403 权限/成员类错误归类为「非成员/无权指派」语义，主文案不再误报「assignee 用户名无效」；trace_id 与上游脱敏原文作为辅助字段（upstream_detail）保留。

## Scope
- `src/api/issues_center.py::create_task_via_issue`（用户建单主通道，`POST /api/issues/{owner}/{repo}` 复用）。
- Out of Scope：GitCode 上游权限模型、Issue update 链路、成员缓存优化、前端选择器、`create_standard_task`（onboarding 路径默认 owner，owner 必为成员，不在本缺陷范围）。

## Technical Approach
- assignee 解析改为 `(body.assignee or "").strip()`，删除「默认 fallback 创建者」分支；创建者登录名单独解析为 `_creator_login`（external_login 优先），仅用于场景归属 issue_creator / pipeline 事件创建人，不回填 assignee。
- 新增 `_gitcode_assignee_member_check()`：GET /repos/{owner}/{repo}/collaborators/{u}/permission，三态 member / not_member(404) / unknown(403/429/网络)。仅 GitCode 且 assignee 非空时调用；not_member → 403 阻断（含 trace_id）；unknown → 放行交上游裁决（避免查询接口自身无权限被误判）。
- 上游 403 权限/成员类分支：assignee 非空 → 「无权指派 assignee（非成员或无权指派）」主文案 + trace_id + upstream_detail；assignee 为空 → 通用上游 403 提示。删除「请检查 assignee 是否为有效 GitCode 用户名」误导措辞。
- 请求级 `trace_id = tr_<uuid16>`。

## Acceptance Criteria（与 Issue Spec 一致）
- [x] 非成员不传 assignee → 创建成功，上游请求 assignee 为空
- [x] 显式传非成员 assignee → 403「非成员」语义错误（含 trace_id），不调用上游
- [x] 上游 403 错误响应含 trace_id 与上游脱敏原文（upstream_detail 字段，非主文案）
- [x] 成员 assignee 场景不回退（预校验 member 放行，正常创建）
- [x] 错误主文案不再出现「assignee 用户名无效」误导措辞
- [x] 新增 tests/test_issue_649_assignee_member_check.py 覆盖 4 个验收场景；更新 test_issue_523 回归
