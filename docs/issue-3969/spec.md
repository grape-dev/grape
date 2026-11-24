# Spec: Issue #3969 — worker 重启 91s 内 /api/runtime/* 全量 502

> 基线 `arch-weekly-11-ljt`（`44b08ea9f`）| 来源：Issue 正文 + Issue Agent Spec v1（comment 203129359）

## Objective
消除「单台 worker 重启即可引发全平台 runtime 控制面中断」的结构性风险。成功标准：
计划内单台重启/升级窗口内 `/api/runtime/*` 不再出现秒级全量 502；对照基线
**1,178 次 / 91s（峰值 42 次/秒、30 客户端 IP）**下降 ≥2 个数量级，且 502 率
>1%/5min 可告警。

## 现象与根因（Loki 2026-11-23 20:00:48–20:02:19，master-002）
- `nginx-gateway` 在 91s 内对 worker 上游 `127.0.0.1:8083` 返回 1,178 次 502，覆盖心跳 /
  注册 / 认领 / 状态 / 消息 / 完成 / 标签全部 runtime 控制面。
- 首个 502 早于 `Application shutdown complete` 57s：uvicorn 收到 SIGTERM 后先关监听
  再排空，随后冷启动 36s；「关监听 → 排空 57s → 冷启动 36s」= 91s 新连接被拒。
- 网关 `upstream grape_worker_server` 仅单 server，无 `proxy_next_upstream` / keepalive；
  单上游拒连只能 502，无法切到共享同一 RDS 的其它主控 worker。

## Scope
### In
1. 网关 `upstream grape_worker_server` 多上游 + 故障切换 + keepalive + 超时（四进程与 cluster 两份配置）。
2. worker 停机先摘流后排空、启动 readiness 门禁（systemd + 进程）。
3. runtime 客户端重试收敛（抖动 / 总时长上限）与「单次 502 vs 连续失败」判定。
4. nginx 层 `/api/runtime/` 502 率指标与 >1%/5min 告警、滚动重启 SOP、SLO 基线文档。

### Out
- runtime 协议/接口重设计、控制面拆库、nginx 替换、跨机房容灾。
- 与本次无关的其它 5xx（#3847 热加载摘流、#3764 错端口）复现治理。

## 现状复核（本基线）
1. `deploy/nginx-grape-four-process.conf:17-19` / `deploy/nginx-grape-cluster.conf:8-10`
   均为单 server，无 next_upstream、无 keepalive。
2. `deploy/systemd/grape-worker-server.service` 无 `ExecStop` 摘流、无 `ExecStartPost`
   readiness，`Restart=always` 直接拉起；`src/api/run.py:45-53` SIGTERM 仅 terminate+join。
3. `src/api/grape_worker_server.py:91` 以 `readiness=False` 挂健康端点；`server_common`
   已有 `/healthz` + `/readyz` 可复用。
4. `src/domains/runtime/engine/client.py:247-327` `_request` 已有 408/429/5xx 指数退避
   （4s→64s，默认 retries=4），但**无抖动、无总时长上限**；`heartbeat-v2` 仅 retries=1、
   v1 retries=2，无「单次 vs 连续失败」观测口径。
5. `health_scorer.py:49` `_HEARTBEAT_STALE_SECONDS=100` 只按 last_seen 年龄判定，未显式
   表达「连续 ≥3 个心跳间隔失联才判离线」。
6. `deploy/alerts.yml:70-79` 只有应用层 5xx >10%/5min，无 nginx 层 `/api/runtime/` 502 率；
   `deploy/alloy-config.alloy` 只把 nginx access.log 送 Loki，未生成指标。

## Acceptance Criteria
- [ ] `nginx -t` 通过；配置可核验含多 server（含对等主控 include）、`proxy_next_upstream`、keepalive。
- [ ] 故障注入：一台上游拒绝连接时 `/api/runtime/*` 请求仍 200（GET 自动切换；幂等 POST
      由网关 `non_idempotent` + 客户端重试覆盖），无持续 502。
- [ ] 计划内 `systemctl restart grape-worker-server` 期间 30 客户端压心跳，502 相对基线
      1,178 次/91s 下降 ≥2 个数量级；日志可核验「先摘流 → 排空 → 关监听 → readiness → 接入」。
- [ ] 单次 502 不触发离线/健康分骤降；连续 ≥3 次失败才判离线（单测覆盖退避序列、抖动上限、连续失败阈值）。
- [ ] `/api/runtime/` 502 率 >1%/5min 告警在测试注入下触发。
- [ ] 滚动重启 SOP 与 SLO 基线（1,178 次/91s、42 次/秒、30 客户端）落库。
- [ ] 现有测试全通过，新增用例覆盖新行为。