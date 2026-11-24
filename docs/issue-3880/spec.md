# Spec: Issue #3880 — c5fc86a162 后 v2 claim 端点全 500（三参错位）

> Spec 主体已由上游 Issue Agent 发布（见 Issue #3880 Spec 评论，评级 ready）。本文为落地侧引用摘要，不重做分析。

## Objective

修复 v2 claim 端点 `POST /api/runtime/runtimes/uid/{engine_uid}/tasks/claim` 全量 500，恢复 v2-first worker 正常领取任务。

## 根因

- `runtime_claim`（`src/domains/runtime/routes/runtime.py:1157`）签名新增第二位置参数 `request: Request`（用于 `_request_base_url` 回填 server_url）。
- 同 commit 未同步 `runtime_claim_v2` 包装（:1632），仍以三参调用 `runtime_claim(rid, _claim_body_v1(body), credential_id)`，形参错位：`_claim_body_v1(body)` 落位到 `request`、`credential_id`(int) 落位到 `body`，执行到 `body.expected_task_id`（:1171）时报 `'int' object has no attribute 'expected_task_id'` → 500。

## 修复方案（上游推荐方案 1）

`runtime_claim_v2` 签名增加 `request: Request`（置于 `engine_uid` 之后），调用改为 `runtime_claim(rid, request, _claim_body_v1(body), credential_id)` 四参透传。

## 验收标准

- v2 uid claim 调用契约测试通过（不再出现 `'int' object has no attribute ...` 形态错位）；
- v1 claim 同参数行为无回归；
- claim-by-tag / claim-bound / claim-by-tag-bound / claim-capabilities v2 变体无回归；
- 补充覆盖 v2 claim 调用契约 / 形参顺序的回归测试。
