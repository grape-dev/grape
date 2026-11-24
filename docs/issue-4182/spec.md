# Issue #4182 Spec — 观测页移除冗余 /health/realtime 轮询

## 目标

让运维观测页 realtime 数据（任务队列 / runtime 在线数 / worker 卡片）在公网登录用户下显示真实值，
并消除每小时约 81 条 `/health/realtime` 403 轮询噪声。

## 背景

- 后端 `src/api/health_endpoints.py` 的 `health_realtime()` 只服务 loopback，公网必 403。
- 仓库内 nginx 配置没有该 location，白名单位于宿主机 `/etc/nginx/snippets`（运维侧）。
- 前端 `src/api/frontend/app/src/views/ObservabilityView.vue` 同源直调 `/health/realtime`，
  结果 `rtData` 只在 `fallbackOps()` 兜底中使用；页面主数据来自受鉴权的
  `/api/dashboard/ops/overview`。

## 方案 A′

1. 删除观测页的 `G.fetch('/health/realtime')` 与 `rtData`；`refreshObs()` 只请求
   `/dashboard/ops/overview`。
2. `fallbackOps()` 在无 overview 时 `tasks` / `runtimes` / `workers` 返回 `null`（显示 `—`），
   不填 0。
3. overview 失败时置 `opsFailedAt`，复用既有 `opsStale` / `obs_stale_since` / `obs_partial` 提示，
   不再静默降级。
4. 全前端排查 `/health/realtime` 其它调用点。

## Out of scope

- 不改 nginx / 宿主机白名单。
- 不改 `/health/realtime` 后端本身（内网 `127.0.0.1:8081` 仍须 200）。
- 不新建 `/api` 等价 realtime 端点。

## 验收

- 前端不再请求 `/health/realtime`，公网 1h 403 计数 = 0。
- 公网登录用户观测页卡片显示真实值；overview 失败显示 `—` 与未更新提示。
- 内网 `curl -sI http://127.0.0.1:8081/health/realtime` 仍 200。
- 前端构建通过，现有测试通过，新增回归测试覆盖 fallback unknown 语义。