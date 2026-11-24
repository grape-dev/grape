# Plan: Issue #3969

## 方案概述
计划内重启走「先摘流 → 排空 → 关监听 → 冷启动 → readiness 通过再接入」；突发上游拒绝由
「网关多上游故障切换 + 客户端幂等重试」兜底。四层改动互不依赖，可独立回滚。

## WP1 网关多上游与故障切换
文件：`deploy/nginx-grape-four-process.conf`、`deploy/nginx-grape-cluster.conf`
- `upstream grape_worker_server/grape_worker` 增加 `include /etc/nginx/conf.d/grape-worker-peers.conf*;`
  （部署注入对等主控 worker 私网端点，缺省无文件 = 仅本机）+ `keepalive 32;`，
  本机 server 加 `max_fails=2 fail_timeout=10s`。
- `/api/runtime/`、`/api/tasks/`（cluster 同名块）加：
  `proxy_http_version 1.1; proxy_set_header Connection "";`
  `proxy_next_upstream error timeout http_502; proxy_next_upstream_tries 2; proxy_next_upstream_timeout 5s;`
- 幂等端点（heartbeat / heartbeat-v2 / register / tasks/{id}/state）用正则 location 加
  `non_idempotent`，其余仅连接级 `error timeout` 重试，避免非幂等写被重复执行。

## WP2 停机摘流与启动门禁
- 新增 `src/api/drain_state.py`：文件型排空标记（env `GRAPE_DRAIN_FILE`），多 uvicorn worker
  共享；`begin_drain/clear_drain/is_draining/seconds`。
- `server_common.attach_health(..., drain_file=...)`：排空期间 `/healthz`/`/readyz` 返回
  503 `not_ready` + `checks.draining`。
- `grape_worker_server.py`：`readiness=True`；lifespan 启动清旧标记；挂 `POST /internal/drain`
  （仅回环，nginx 已 deny `/internal/`）。
- `deploy/systemd/grape-worker-server.service`：`ExecStop` 调摘流脚本（标记 + sleep
  `GRAPE_DRAIN_SECONDS` ≥5s），`ExecStartPost=-` readiness 轮询（失败仅记录，不阻断，
  避免 Restart=always 抖动），调大 `TimeoutStartSec/TimeoutStopSec`。
- `src/api/run.py`：SIGTERM 时若配置 `GRAPE_DRAIN_FILE` 先 `begin_drain()` 并等待
  `GRAPE_DRAIN_SECONDS`，再 terminate + join（与 uvicorn graceful 顺序一致）。

## WP3 客户端重试收敛与失败判定
文件：`src/domains/runtime/engine/client.py`、`health_scorer.py`
- `_request` 退避加抖动（`GRAPE_CLIENT_RETRY_JITTER`，默认 ±20%）与总时长上限
  （`GRAPE_CLIENT_RETRY_BUDGET_SECONDS`，默认 90s），避免惊群与无限拉长。
- 新增连续失败计数 `consecutive_failures` / `heartbeat_fail_streak`：单次请求成功即清零，
  只有「重试耗尽/网络错误」才 +1；单次 502 被重试覆盖，不直接判离线。
- heartbeat v2 retries 1→2、v1 retries 2→3，幂等端点 ≥3 次重试。
- `health_scorer` 显式 `_heartbeat_stale_threshold()`：`max(stale, interval × misses)`，
  默认 `max(100, 15×3)=100` 保持现网；测试可注入小值验证「1 次不失联 / 3 次判离线」。

## WP4 可观测与发布编排
- `deploy/alloy-config.alloy`：`loki.process` 从 nginx access.log 提取 status/path，
  生成 `nginx_upstream_502_total` 与 `nginx_upstream_requests_total`。
- `deploy/alerts.yml`：`/api/runtime/` 502 率 >1%/5min（warning）。
- `deploy/README.md`：滚动重启 SOP（逐台、等 502 归零再下一台）。
- `docs/deployment/worker-restart-slo.md`：记录 1,178 次/91s、42 次/秒、30 客户端基线。

## 关键决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 多上游拓扑 | 本机 + 对等主控（include 注入） | 四进程共享 RDS 可互备；同机多端口无法隔离进程级故障 |
| POST 重试 | 仅幂等端点 `non_idempotent` | 避免非幂等写重复执行 |
| 摘流方式 | systemd ExecStop 前置摘流 + 排空 | nginx reload 本身可抖动（#3847） |
| 502 指标来源 | Alloy `loki.process` 日志指标 | 复用现有采集链路，不新增组件 |
| readiness | 复用 `attach_health(readiness=True)` | 已有 `/healthz`+`/readyz` 实现 |

## 测试 seam
- nginx/systemd/alert/SLO 静态配置契约（文本断言 + `nginx -t` 语法校验）。
- `drain_state` 纯函数 + `attach_health` 在最小 FastAPI app 上的 `/readyz` 行为。
- `RuntimeClient._request` 退避序列/抖动范围/预算（mock aiohttp session）。
- `health_scorer._heartbeat_stale_threshold()` 与 `_fetch_heartbeat` 的阈值口径。