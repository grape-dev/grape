# Issue #4182 Plan — 观测页去除冗余 realtime 轮询

## 代码改动

| 文件 | 改动 |
|------|------|
| `src/api/frontend/app/src/views/ObservabilityView.vue` | 删除 `rtData` 与 `/health/realtime` 请求；`fallbackOps()` 未知语义；失败置 `opsFailedAt`；任务 KPI 走 `kpiVal` |
| `src/api/frontend/shell/index.html` | `observability-view.js?v=` 59 → 60（源变更抬缓存版本） |
| `tests/api/test_frontend_bundle_source_ratchet.py` | 同步 observability chunk 的页面版本与源指纹 |
| `tests/insight/test_issue_4182_obs_no_realtime_poll.py` | 新增回归：前端无 `/health/realtime`、fallback 全 null、内网门禁保留 |

## 数据流

- 轮询：`refreshObs()` → `GET /api/dashboard/ops/overview`（受鉴权）→ `opsData`。
- 失败：保留上一份有效快照并记录 `opsFailedAt`；无快照时 `fallbackOps()` 全 null。
- 渲染：`opsWorkers` / `opsRuntimes` 对 null 与后端 `degraded` 段统一输出 `—`。

## 风险

- 首屏无 overview 时显示 `—` 而非 0：符合"未知不是 0"的既有约定（#3592）。
- 缓存：`?v=` 抬档并同步棘轮，避免旧 chunk 继续轮询。