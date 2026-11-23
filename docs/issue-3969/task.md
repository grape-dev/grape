# Task: Issue #3969

| # | 切片 | 产出 | 提交 |
|---|---|---|---|
| 1 | 文档 | `docs/issue-3969/{spec,plan,task}.md` | `docs(#3969)` |
| 2 | 失败测试（红） | `tests/contracts/test_issue_3969_*.py`、`tests/runtime/test_3969_*.py` | `test(#3969)` |
| 3 | WP1 网关 | `deploy/nginx-grape-{four-process,cluster}.conf` | `feat(#3969)` |
| 4 | WP2 摘流/readiness | `src/api/{drain_state,run,server_common,grape_worker_server}.py`、`deploy/systemd/*` | `feat(#3969)` |
| 5 | WP3 客户端/健康 | `src/domains/runtime/engine/{client,health_scorer}.py` | `feat(#3969)` |
| 6 | WP4 可观测/SOP/SLO | `deploy/{alloy-config.alloy,alerts.yml,README.md}`、`docs/deployment/worker-restart-slo.md` | `feat(#3969)` |
| 7 | 回归 + 自审查 + PR | pytest / PR | — |

验收钩子：`nginx -t` 通过；WP1–WP4 静态契约用例全绿；client/health 单测覆盖退避、抖动、连续失败阈值。