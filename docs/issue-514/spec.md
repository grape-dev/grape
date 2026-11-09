# Spec: Issue #514 — /webhook/gitcode 非 POST 方法返回 405

> 来源：Issue Agent Spec（目标分支 weekly-09）

## 目标
`/webhook/gitcode` 对非 POST 方法（GET/PUT/DELETE/PATCH/OPTIONS/HEAD）显式返回 405 Method Not Allowed，不得落入 SPA catch-all 兜底返回 200 HTML。

## 现状与根因
- 路由仅声明 `@router.post("/webhook/gitcode")`（src/api/routes/webhook.py）。
- 非 POST 请求不命中任何注册路由，被 `main.py spa_catch_all`（`GET /{path:path}`）兜底为 200 text/html。
- 影响：健康探针/监控把入口误判为成功。

## 验收标准
- `pytest tests/atomic -m atomic -k webhook_entry_rejects_non_post_methods` 通过
- GET/PUT/DELETE/PATCH/OPTIONS/HEAD → 405，非 200，非 text/html
- POST 行为不变（空 body 仍 400 invalid body）

## Out of Scope
- 其他路由的 SPA 兜底行为；POST webhook 处理逻辑；反代层方法过滤。
