# Plan: Issue #514 — 实现方案

## 测试 seam
公开 HTTP 接口 `TestClient(app)` 对 `/webhook/gitcode` 的方法级响应（状态码 + content-type）——复用已有 FastAPI app seam，不新建内部 seam。

## 实现路径（TDD 红/绿）
1. **红**：`tests/atomic/test_webhook_entry_non_post.py` — `test_webhook_entry_rejects_non_post_methods`，断言非 POST → 405 非 HTML；POST 空 body → 400 不变。基线上复现失败（GET 实际 200）。
2. **绿**：`src/api/routes/webhook.py` 新增 `@router.api_route("/webhook/gitcode", methods=[GET,PUT,DELETE,PATCH,OPTIONS,HEAD], include_in_schema=False)` 处理器，统一返回 `JSONResponse(405, {"code":405,"message":"method not allowed: webhook endpoint accepts POST only"})`。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 状态码 | 405（非 404） | Method Not Allowed 语义最准确 |
| 实现方式 | api_route 多方法单处理器 | 显式、集中、与现有 @router.post 风格一致 |
| OPTIONS 纳入 | 是 | 防止 OPTIONS 也落入 SPA 兜底 |
| HEAD 纳入 | 是 | FastAPI 不自动派生 HEAD，不显式声明会落入 catch-all |

## 风险
- 路由优先级：webhook router 注册先于 main.py 末尾的 SPA catch-all，显式路由优先命中，无冲突。
- 回归：基线对照（stash 前后）确认无新增失败。
