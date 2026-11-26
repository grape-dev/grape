# Plan: Issue #4445

## 改动清单

| 模块 | 改动 |
|---|---|
| `src/domains/runtime/engine/cleanup.py` | 新增 `_sweep_tmp_task_residuals()` / `tmp_janitor_metrics_snapshot()`，挂进 `_sweep_tmp_residuals()`（sweep_once 既有链路）；活跃任务归属识别 `_tmp_janitor_owned_by_active()`；`_sweep_local_tmp_once()` 供未持集群锁的节点跑本机清理 |
| `src/domains/runtime/engine/state_db_maintenance.py` | `worker_metrics_snapshot()` 合并 janitor 累计量 |
| `src/domains/runtime/routes/runtime.py` | `HeartbeatBody` 增 2 列并写入 UPDATE |
| `src/domains/runtime/migrations/tmp_janitor_metrics_columns.py` | 新增注册式迁移（safe + post_check） |
| `src/api/db_setup.py` | 注册迁移 `202611250014` |
| `src/domains/insight/observability/tmp_usage.py` | 新增 tmpfs 水位 + janitor 指标 install/collect；**节点侧** `grape_node_tmp_*` 由心跳落库行采样 |
| `src/domains/runtime/public/__init__.py` | 登记 `runtime_credentials_tmp_rows`（insight 采样用 owner 读口） |
| `src/domains/insight/observability/__init__.py` | 注册 install + on_metrics_scrape 采样；`METRICS_ADDR` 已设但缺 `prometheus_client` 时显式 warning |
| `deploy/alerts.yml` | 节点心跳通路 `grape_node_tmp_used_ratio` + 控制面本机通路 `grape_tmpfs_used_ratio` 各 2 条（>80% warning / >90% critical） |
| `src/domains/runtime/engine/engine_select.py` | `_filter_by_tmp_headroom()`，接入档位选择与健康兜底；水位按候选凭据一次预取复用 |

## 安全边界

- 前缀白名单（收敛后）：默认 `qa[0-9]*,qa-*,pr[0-9]*,pr-*,verify[0-9]*,verify-*,base[0-9]*,rev[0-9]*,head[0-9]*,deploy-ws,venv[0-9]*,venv-*,pkg-*,wt-*`，
  `SWEEPER_TMP_PATTERNS` 可覆盖；`baseline*`/`review*`/`headless*` 等第三方目录不再命中。
- TTL 默认 7200s（`SWEEPER_TMP_TTL_SECONDS`）；`SWEEPER_TMP_JANITOR=off|dry|on`，未知拼写落 `dry`；
  env 误配（空串/非数字）回落默认值并留 warning，不在导入期崩溃。
- 符号链接不跟随、不删除；活跃任务目录（`qa-<task_id>`、`pr<task_id>-head` 等归属形态）、
  当前 `TASK_TMPDIR`/会话暂存根豁免；活跃任务探测失败 → 本轮整体跳过（fail-closed）。
- `dry` 只统计不删除，且不累加 `tmp_janitor_*_total`（避免看板/告警假阳性）。
- 水位门 fail-open：查询异常/缺列不挡派发；本机直连（credential_id=0）不参与。

## 指标通路（为什么需要两条）

远端 worker 最小依赖不含 `prometheus_client`（Issue #2923），`src.domains.runtime.engine.agent`
进程也**没有** `/metrics` 端点——只靠 worker 侧 gauge，节点 /tmp 告警永远不会触发。

1. **节点通路（主）**：worker 心跳把 `tmp_percent` 与 janitor 累计量写入 `runtime_credentials`
   （迁移 202611250014）→ 控制面 `/metrics` scrape 时按 host 采样为
   `grape_node_tmp_used_ratio{host}` / `grape_node_tmp_janitor_removed_items_total{host}` /
   `grape_node_tmp_janitor_removed_bytes_total{host}`（同 Issue #3773 的 `grape_acp_state_db_*`）。
2. **控制面本机通路**：`grape_tmpfs_used_ratio{mount}` / `grape_tmp_janitor_*`，仅在装了
   `prometheus_client` 且配置了 `METRICS_ADDR` 的进程上导出。

上线验收：在控制面 `/metrics` 抓 `grape_node_tmp_used_ratio` 有值（覆盖所有在线 worker）；
在任一配置了 `METRICS_ADDR` 的进程抓 `/metrics` 能看到 `grape_tmpfs_used_ratio{mount="/tmp"}`。
若日志出现 `METRICS_ADDR=... set but prometheus_client is unavailable`，说明该进程只装了 no-op 桩，
`/metrics` 为空 body —— 装 `prometheus_client` 或清空 `METRICS_ADDR`。

## 测试

- `tests/runtime/test_issue_4445_tmp_janitor.py`（活跃归属/`qa-<id>` 豁免、fail-closed、dry 不累加指标、env 容错、per-host 清理）
- `tests/runtime/test_issue_4445_tmp_headroom.py`
- `tests/api/test_issue_4445_tmp_metrics_columns.py`
- `tests/insight/test_issue_4445_tmp_node_metrics.py`（心跳落库 → 控制面序列）
- `tests/deploy/test_issue_4445_tmp_alerts.py`（两条通路的告警规则）