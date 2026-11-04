# Plan: Issue #885 — 实现方案

## 服务端（src/api/routes/scenes.py）

1. **GET /{scene_id}/advanced-options 收敛**
   - `is_secret=true` 字段：`value` 置空串、新增 `has_value: bool(原value)`；非 secret 字段保持明文返回（原行为）。
2. **新增 POST /{scene_id}/advanced-options/reveal**（复用 #474 settings.py 的限流表与辅助函数）
   - 请求体：`{name: str, password: str}`（SceneAdvancedOptionsRevealBody）。
   - 前置：repo 解析（400）→ `check_repo_access`（403）→ 场景存在（404）→ 字段存在且 `is_secret`（400/404）。
   - 二次确认：重新输入 Grape 密码（`verify_password`，未设密码 → 400 + need_set_password）。
   - 高频限流：复用 `grape_t_credential_reveal_log`（provider=`scene_advanced_options`，field=变量名），窗口阈值同 #474 → 429。
   - 审计：`log_audit("scene_secret_reveal", ...)`，detail 含 scene_id/repo_key/name，不落明文。
   - 响应：`{name, value}`，`Cache-Control: no-store`。

## 审计（src/auth/audit.py）
- EVENT_TYPES 新增 `scene_secret_reveal`。

## 前端（src/api/static/js/pages/dashboard.js）
- `_addCustomFieldInline` 增加 `has_value` 透传：secret 且 `value==""` 且 `has_value` → 输入框 placeholder 显示 `********`（提示已配置），value 留空。
- 保存链路不变：空 secret 值 → 服务端保留旧值（scenes.py 既有 keep 逻辑）。

## 文档
- help/api/06-scenes.md：GET 出参说明更新 + 新增 reveal 端点块（满足 api-docs-sync 契约测试）。

## 测试（tests/）
- 新增 `tests/test_issue885_scene_secret_reveal.py`：占位返回 / reveal 密码确认 / 未设密码 / 非 secret 拒绝 / 字段不存在 / 审计不落明文 / 429 限流 / 写入侧保留旧值回归。
- 更新 `tests/test_scene_env.py`：明文回显断言改为占位断言（has_value）。
