# Issue #4345 任务拆解

| # | 任务 | 产出 | 验证 |
|---|------|------|------|
| T1 | 共享基址解析 | `src/api/a2a_dispatch.py`：`resolve_a2a_dispatch_target` | 三条 resolver 用例（回退未验证/验证通过/可信不探测） |
| T2 | 派发与错误分类 | `dispatch_a2a` + `A2AUpstreamError`/`A2ATransportError`/`A2AUnavailable` + `classify_dispatch_http_status` | 4xx→422、5xx/传输→502 用例 |
| T3 | takeover 接线 | `takeover._a2a_dispatch_base` 调共享解析器 | `tests/test_takeover_dispatch.py` 14 passed |
| T4 | 重派 A2A 分支 | `events._redispatch_a2a_event`：候选探测、未验证短路、逐档映射 | #4345 三路径用例 |
| T5 | 幂等回放 | `_redispatch_replay_lookup/store` + `_run_redispatch` 回放闸 | 成功/失败回放各一条用例 |
| T6 | 审计口径 | `_audit_admin_action(extra=...)`；失败 `dispatch=failed`，成功 `dispatch=ok` | 审计字段用例 |
| T7 | 日志单点 | `_redispatch_log_failure`（seq 为空不落行） | caplog 断言唯一 error 行 |
| T8 | 文档 | 本目录 spec/plan/task | — |
| T9 | 门禁 | #4345 用例 + takeover/redispatch 回归 + `make lint` + `scripts/check-arch.py` | 全绿（基线已知红除外） |

依赖顺序：T1/T2 → T3/T4 → T5/T6/T7；T8 可并行。