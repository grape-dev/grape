# Plan: Issue #4445

## 改动清单

| 模块 | 改动 |
|---|---|
| `src/domains/runtime/engine/cleanup.py` | 新增 `_sweep_tmp_task_residuals()` / `tmp_janitor_metrics_snapshot()`，挂进 `_sweep_tmp_residuals()`（sweep_once 既有链路） |
| `src/domains/runtime/engine/state_db_maintenance.py` | `worker_metrics_snapshot()` 合并 janitor 累计量 |
| `src/domains/runtime/routes/runtime.py` | `HeartbeatBody` 增 2 列并写入 UPDATE |
| `src/domains/runtime/migrations/tmp_janitor_metrics_columns.py` | 新增注册式迁移（safe + post_check） |
| `src/api/db_setup.py` | 注册迁移 `202611250014` |
| `src/domains/insight/observability/tmp_usage.py` | 新增 tmpfs 水位 + janitor 指标 install/collect |
| `src/domains/insight/observability/__init__.py` | 注册 install + on_metrics_scrape 采样 |
| `deploy/alerts.yml` | >80% warning / >90% critical 两条规则 |
| `src/domains/runtime/engine/engine_select.py` | `_filter_by_tmp_headroom()`，接入档位选择与健康兜底 |
| `src/domains/runtime/engine/scheduler.py` | 任务级绑定随机选机 / 绑定探测同样过水位门 |

## 安全边界

- 前缀白名单：默认 `qa*,pr*,verify*,base*,rev*,deploy-ws,venv*,pkg-*,head*,wt-*`，
  `SWEEPER_TMP_PATTERNS` 可覆盖；第三方文件不碰。
- TTL 默认 7200s（`SWEEPER_TMP_TTL_SECONDS`）；`SWEEPER_TMP_JANITOR=off|dry|on`。
- 符号链接不跟随、不删除；活跃 task_id 目录与当前 `TASK_TMPDIR`/会话暂存根豁免。
- 水位门 fail-open：查询异常/缺列不挡派发；本机直连（credential_id=0）不参与。

## 测试

- `tests/runtime/test_issue_4445_tmp_janitor.py`
- `tests/runtime/test_issue_4445_tmp_headroom.py`
- `tests/api/test_issue_4445_tmp_metrics_columns.py`