# Spec: Issue #683 — 422 校验错误响应携带字段级详情

> 来源：Issue Agent Spec（评论 id 197570399）| 目标分支：weekly-08-mys（环境注入基线）

## 目标
`GET /api/issues/{owner}/{repo}` 非法 `status`（如 all/open）返回的 422 响应体为 `{code:422, message}` 信封，message 携带非法字段名与合法值枚举 `[active, processing, awaiting_input, pending_review, completed, cancelled]`。

## 根因
- `src/api/main.py` 仅注册 `Exception`→500 handler，缺 `RequestValidationError` handler，422 走 FastAPI 默认 list-detail。
- `src/api/routes/issues.py:1858` `status` Query pattern 正确但错误详情未透传。

## 范围
- In：main.py 注册全局 `RequestValidationError` handler；errors.py 新增 `validation_error_response()`（从 pattern 解析合法值）。
- Out：grape-cli help 合法值枚举、client.py 422 detail 解析（跨仓 follow-up，grape-dev/grape-cli）。

## 验收标准
- 非法 `--status` 值的 422 含字段级详情与合法值枚举
- 合法值行为不回归
- 新增测试覆盖；现有测试全通过
