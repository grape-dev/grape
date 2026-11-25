# Issue #3354 Spec：仓库自动托管增加有限重试与失败可见性

> 来源：Issue #3354｜目标分支 `weekly-mys`｜沿用上一流水线已发布 Spec

## 背景

`POST /api/repos/create` 携带 `host_after_create=true` 时，仓库刚在 VCS 侧创建、
尚未就绪，托管链路调用 find/setup webhook 立即返回「仓库不存在或无权访问」，
落库 `host_status=partial`、`webhook_id=0`。用户工作台因此显示未托管；
webhook 缺失导致 PR opened/merged 事件不回流、`pull_request_merged` 阶段不触发。
手动执行一次 `grape repos host` 即可成功，确认是建仓与托管之间的时序竞态。

## 目标

- 建仓后自动托管对 VCS 就绪时序错误做有限退避重试（3 次，5s/15s/45s）；
- 仅对「仓库不存在或无权访问」类时序性错误重试，权限/限流/参数类快速失败；
- 终态失败将具体原因 + 重试次数写入 `host_error`，`host_status` 维持失败态；
- 失败路径复用已有幂等 host 语义（find 复用已有 webhook、setup 建缺失 webhook）；
- 前端工作台托管状态组件展示失败原因与「托管失败，点击重试」一键重试。

## 范围

### In Scope
- `src/domains/mgmt/routes/repos.py`：`create_new_repo` 托管分支的重试循环与终态落库；
- `src/domains/insight/routes/dashboard.py`：dashboard 透出 `host_step` / `host_error`；
- `src/api/frontend/app/src/views/DashboardView.vue`：失败态文案与重试按钮；
- `tests/mgmt/test_issue_3354_host_after_create_retry.py`：重试/退避/失败落库回归。

### Out of Scope
- VCS 侧建仓就绪时序本身（外部依赖，只做退避消化）；
- PR 关联/webhook 事件回流的其他历史缺陷（webhook 建好后自然恢复）。

## 验收标准

- [x] mock VCS 延迟就绪：前 3 次 404 → 3 次重试（5s/15s/45s）后成功，
      终态 `webhook_id>0` 且 `host_status` 清理为完整（空串）；
- [x] 重试耗尽：`host_error` 含具体原因与「已重试 3 次」，`host_status=webhook_missing`；
- [x] 权限类错误（403）不重试，`host_error` 含「已重试 0 次」；
- [x] 失败时前端出现失败原因与「托管失败，点击重试」，复用幂等 repair-host。