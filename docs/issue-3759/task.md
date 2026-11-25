# Issue #3759 — 任务拆解与落地清单（task）

| # | 任务 | 状态 | 证据 |
|---|------|------|------|
| T1 | 窗口日志去重原语（kernel） | done | `src/kernel/log_dedupe.py`；`tests/kernel/test_issue_3759_log_dedupe.py`（8 例） |
| T2 | 永久失败判定 + 退避档位 | done | `is_permanent_status` / `backoff_seconds`；`TestClassification`（14 参数化例） |
| T3 | 仓库级存在性探测 + 台账列 + 幂等迁移 | done | `probe_repo`；`migrations/repo_vcs_health_columns.py`（`202611250001`） |
| T4 | 熔断判定与短路（`_vcs_get_issue` / `issues get` 路由） | done | `TestProbeAndFuse` / `TestReadPathShortCircuit` |
| T5 | 自动恢复 + 运维 reset 入口 + 列表字段 | done | `note_recovery` / `POST vcs-health/reset` / `/managed` 的 `vcs_*` |
| T6 | `_vcs_req` 与 `api_client` 日志降噪 | done | `TestUpstreamReqDedupe`（含 429 不标 permanent 与状态码透传回归） |
| T7 | 文档（spec/plan/task） | done | `docs/issue-3759/*` |

## 回归命令

```bash
python3 -m pytest tests/kernel/test_issue_3759_log_dedupe.py \
                  tests/runtime/test_issue_3759_vcs_breaker.py \
                  tests/mgmt/test_issue_3759_repo_vcs_health_columns.py -q
python3 -m pytest tests/runtime/test_repogit.py \
                  tests/runtime/test_issue_3482_workspace_order.py \
                  tests/runtime/test_issue_2245_git_writable.py -q
make lint && python3 scripts/check-arch.py
```

## 基线已知红（与本改动无关，已在 PR 说明中列明）

- `tests/kernel/test_gsb_route.py::test_public_primitives_registry_sync`：`PUBLIC_PRIMITIVES`
  与 public 实际面存在 40 项历史漂移（本 Issue 只新增 2 项且已登记）。
- `tests/runtime/test_issue_1819_account_env_injection.py`（2 例）、
  `tests/runtime/test_issue_1005_milestone_resolution.py::test_milestones_list_adds_id_from_url`：
  `open_with_policy` / milestone 归一的历史失败，改动前后一致。
- `tests/api/test_api_routes.py`（13 例）、`tests/mgmt/test_2121_repo_cache_invalidation.py`（4 例）：
  `src.kernel.token_resolver` 缺 `create_provider` 的历史漂移，改动前后同为 19 failed / 124 passed。