# Spec: Issue #885 — 场景自定义变量密钥字段脱敏（占位 + 按需取回）

> 对齐 Issue #474（credentials reveal 修复模式）。目标分支：weekly-mys

## 目标
`GET /api/scenes/{scene_id}/advanced-options` 对 `is_secret=true` 的密钥类字段不再明文返回，改为占位（`value:""`, `has_value:true/false`）；按需取回明文走独立 reveal 接口（二次确认 + 审计日志 + 高频限流）。

## 范围
- 服务端：GET 响应收敛（is_secret 字段 value 置空 + has_value 标记）。
- 服务端：新增 `POST /api/scenes/{scene_id}/advanced-options/reveal`（密码二次确认 + 审计 + 当次返回 no-store + 窗口限流 429）。
- 前端：dashboard.js 高级选项回填与占位语义对齐（secret 空 value + has_value → 掩码占位提示；保存空 secret 值沿用既有"保留旧值"逻辑）。
- 审计：新增 `scene_secret_reveal` 事件类型。

## 非范围
- 个人凭据接口（#474 已覆盖）、密钥存储加密、autopilot 任务自身 custom_fields。

## 验收标准
- GET 响应中 is_secret=true 字段不再出现明文 value；
- 密钥字段以占位形式返回（has_value 标记），前端掩码对齐；
- reveal：独立接口 + 密码二次确认 + 审计日志（谁/何时/哪个字段，不落明文）+ 429 高频限制；
- 写入/更新流程不受影响（secret 空值回传 → 服务端保留旧值，已有逻辑）；
- 新增测试覆盖，现有测试更新后全通过。
