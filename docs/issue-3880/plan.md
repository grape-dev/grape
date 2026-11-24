# Plan: Issue #3880

## 改动点（1 个函数签名 + 1 处调用透传）

1. `src/domains/runtime/routes/runtime.py` `runtime_claim_v2`：签名增加 `request: Request`（置于 `engine_uid` 之后，与 v1 `runtime_claim` 的形参顺序对齐：`(runtime_id, request, body, credential_id)`）。
2. 调用改为四参透传：`runtime_claim(rid, request, _claim_body_v1(body), credential_id)`。
3. 回归测试（`tests/api/test_issue_1011_claim_v2.py` / `tests/api/test_issue_1011_integration.py`）：现有直调用例补传 request；新增签名契约守卫测试（v1/v2 前两形参 + `request` 注解为 `fastapi.Request`），锚死形参顺序防错位复发。

## 影响面

- 仅 v2 claim 主端点包装层；服务层 `service.claim` 语义零改动。
- v1 claim / claim-by-tag / claim-bound / claim-by-tag-bound / claim-capabilities 端点不动。
- 路由直调测试需按新签名传 request（FastAPI 注入路径行为不变）。

## 风险与回退

- 风险低：HTTP 层由 FastAPI 自动注入 request，不改变 wire 协议。
- 回退：单一 commit revert 即可。
