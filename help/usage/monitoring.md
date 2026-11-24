# 9. 系统监控

## 运维概览

**系统监控** 页面提供实时运维数据：

- 任务队列深度（排队/运行中/已完成/失败）
- 卡住检测（运行超过 22 分钟的任务预警）
- Runtime 引擎负载（运行任务数/最大并发数）
- 引擎探活（每 10 分钟 CLI 测试，显示延迟和状态）
- Worker 设备健康（CPU/内存/磁盘/tmp 使用率）

## 引擎探活

系统每 10 分钟对每个引擎发起测试对话（如 `opencode run "hello"`），记录响应延迟和成功/失败状态。探活结果在 Runtime 引擎表格中以图标显示：

- ✅ ok + 延迟（秒）— 引擎正常
- ❌ fail + 错误信息 — 引擎异常
- ⏳ pending — 等待首次探活
- ⏭ skip — 引擎未安装

## 网关 502 指标与告警（Issue #3969）

Alloy 从 `nginx access.log` 派生以下 Prometheus 指标（由 Alloy 自身 `/metrics`
暴露并自抓取；见 `deploy/alloy-config.alloy`）：

| 指标 | 含义 |
|---|---|
| `nginx_upstream_requests_total{path,status}` | 发往 worker runtime 控制面的请求数 |
| `nginx_upstream_502_total{path,status}` | 网关层 502 数（按 `status="502"` label 过滤） |

`path` label 中的 runtime_id/task_id 已归一为 `<id>`，避免基数随业务增长。

告警（`deploy/alerts.yml`）：

- `GrapeRuntime502RateHigh`：`/api/runtime/*` 5 分钟 502 率 >1%，`for: 5m`。
- `GrapeRuntime502Burst`：`/api/runtime/*` 2 分钟 502 增量 >50，`for: 1m`（短窗绝对量，
  覆盖 91s 级事故与低流量端点被 `clamp_min` 稀释的漏报）。

验证：`promtool check rules deploy/alerts.yml`；自抓取可达性
`curl -s http://127.0.0.1:12345/metrics | grep nginx_upstream_`。
