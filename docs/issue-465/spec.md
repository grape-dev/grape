# Spec: Issue #465 — runtimes 表「所属设备」列设备名与 Workers 卡片不一致

> 基线 Spec 由 Issue Agent 发布（Issue #465 评论），本文件为开发侧落地版。

## 目标
`/dashboard/ops/overview`（src/api/routes/dashboard.py）runtimes 行 `device_name` 取值修正：
- 远端引擎（credential_id>0）：优先 `runtime_credentials.name`（用户自定义凭据名，与 Agent 运行时页 Workers 卡片标题一致），为空回退 `runtime_credentials.device_name`。
- 本机直连引擎（credential_id=0）：沿用 `host_name` 逻辑，不变。

## 范围
- In：dashboard.py runtimes 查询与行构造；新增取值回退测试。
- Out：Workers 卡片取值（已正确）、instance_name 派生、error-logs 来源名、schema 变更。

## 验收
- 远端引擎行 device_name == rc.name；rc.name 为空时 == rc.device_name（不出现空列）。
- 本机直连引擎行 device_name == host_name（回归不受影响）。
- 前端 observability.js 渲染（rt.device_name）与 `_filterRuntimes` 筛选字段不变即自动一致（核对确认）。
