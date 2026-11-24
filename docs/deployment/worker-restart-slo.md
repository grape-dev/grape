# Worker 重启可用性基线（SLO）— Issue #3969

## 事故基线（2026-11-23 生产 master-002）

| 指标 | 基线值 |
|---|---|
| 中断窗口 | **91s**（20:00:48–20:02:19） |
| `/api/runtime/*` 502 | **1,178 次** |
| 峰值 | **42 次/秒** |
| 受影响客户端 IP | **30 个** |
| 上游 | `127.0.0.1:8083`（单实例，网关无故障切换） |
| 心跳丢失 | 556 次 |
| 窗口内心跳/注册/认领/任务态/消息/完成/标签 | 全部 502 |

路径分布（Top）：heartbeat 325、heartbeat-v2 231、tasks/{id} 154、tags 134、
register 66、state 42、messages 37、recover-orphans 18。

## 修复后的 SLO

| SLO | 目标 | 度量 |
|---|---|---|
| 计划内单台 worker 重启窗口 `/api/runtime/*` 502 | 相对基线 **1,178 次/91s 下降 ≥2 个数量级**（≤ 11 次/91s） | Loki nginx access.log 502 计数 |
| `/api/runtime/` 502 率 | **≤ 1% / 5min**（超阈触发 `GrapeRuntime502RateHigh`） | `nginx_upstream_502_total / nginx_upstream_requests_total` |
| 重启窗口 runtime 控制面可用性 | GET 自动切换；幂等 POST 由网关 `non_idempotent` + 客户端退避重试覆盖，无持续 502 | 故障注入 + 30 客户端心跳压测 |
| 停机顺序 | 先摘流 → 排空 ≥5s → 关监听 → readiness 通过 → 接入 | worker 日志 / `/readyz` / 摘流标记 |

## 保障措施
1. 网关：worker upstream 多实例（本机 + 对等主控，共享 RDS 可互备）、
   `proxy_next_upstream error timeout http_502`、`max_fails=2 fail_timeout=10s`、keepalive。
2. 停机：systemd `ExecStop` 先 `POST /internal/drain` 标记排空（`/readyz` 503），
   等待 `GRAPE_DRAIN_SECONDS`（默认 5s）后再 SIGTERM。摘流标记按角色隔离：worker 用
   `GRAPE_DRAIN_FILE`，main+webhook（`run.py`）用 `GRAPE_MAIN_DRAIN_FILE`，两者不共享，
   避免停 main 把 worker 打成永续 not_ready。
3. 启动：`ExecStartPost` 轮询 `/readyz`；超时仅告警（`-` 前缀），不放大成重启风暴。
4. 客户端：心跳/注册/完成等幂等端点 ≥3 次指数退避（±20% 抖动、总时长上限 90s）；
   单次 502 不判离线，连续 ≥3 个心跳轮次失败才判离线。
5. 发布：逐台滚动，每台等 5min 502 率归零/无新 502 再下一台（见 `deploy/README.md`）。

## 复测方法
- 故障注入：将一台上游改为拒绝连接，确认 `/api/runtime/*` GET 200（自动切换）。
- 计划内重启：30 客户端持续压心跳，`systemctl restart grape-worker-server`，
  统计 91s 窗口 502 数并与 1,178 对比。
- 指标自检：`curl -s http://127.0.0.1:12345/metrics | grep nginx_upstream_` 必须有
  `nginx_upstream_502_total` / `nginx_upstream_requests_total`（Alloy 自抓取段
  `prometheus.scrape "alloy_self"`（127.0.0.1:12345）已启用）。
- 告警验证：按 `deploy/README.md`「502 告警验证」注入 502 日志，确认
  `GrapeRuntime502Burst`（2m 内 >50 次）与 `GrapeRuntime502RateHigh`（5min 率 >1%）
  在规定窗口内触发；`promtool check rules deploy/alerts.yml` 通过。