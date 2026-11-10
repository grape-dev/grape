# Spec: Issue #1691 — a2a handoff 405 指引与派发校验

> 目标基线：weekly-mys；服务端路由/405 信封/ops 探测已由 PR #1071 覆盖，本单只补差量。

## 目标

- `grape a2a handoff` 在注入 base 指向主服务（业务 API 进程，如 `<main-port>`）时不再返回裸 405：命中 405 时 CLI 渲染服务端 `detail.message` 给出可执行 base_url 指引，地址不在客户端硬编码。
- runbook 明确主服务（业务 API，如 `<main-port>`）/事件服（webhook + a2a，如 `<event-port>`）/公共网关的职责边界、默认 base 来源与 handoff 推荐 base。
- handoff 成功后校验 `seq > 0` 与下游派发状态；`status=failed` 或未建事件时显式报错，不静默返回成功。

## 范围

### In Scope

- grape-cli：a2a 路径 405 提示增强 + 可配置 fallback base（`GRAPE_A2A_FALLBACK_BASE_URL` / `a2a_fallback_base_url`）；handoff 响应校验（`status` / `seq` / `event.status` / `/api/a2a/status` 回查）。
- grape：成功 handoff 响应补充 `event`（seq/status/agent/issue_id），便于 CLI 直接读取下游派发状态（与 #710 联动）。
- grape-skills：coding-cli runbook 增加三服职责边界、默认 base 来源、推荐 handoff base、成功/失败校验口径。

### Out of Scope

- 重新实现 PR #1071 已覆盖的 A 进程 a2a_router 注册、405 detail.message 信封、ops a2a-health 探测与 12-a2a.md 文档。
- 回退三进程拆分；`/webhook/*` 仍归事件服。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 自动切服 vs 明确提示 | 默认提示；fallback base 由配置开启 | 避免隐式切服导致业务命令误发到事件服 |
| 地址来源 | 服务端 `detail.message` / 配置注入 | 地址随部署变化，客户端不硬编码 |
| 派发校验 | 优先读响应 `event`，缺失时回查 `/api/a2a/status` | 兼容旧服务端，失败显式报错 |

## 验收标准

- [ ] base 指向未注册 a2a 路由的主服务时，CLI 输出服务端 `detail.message`（含可用 base_url 指引）且不抛裸 405。
- [ ] 配置 fallback base 后，405 自动重试一次并返回成功结果。
- [ ] handoff 返回 `status=failed` 或 `seq=0` 且无 skipped/queued 时 CLI 显式报错。
- [ ] handoff 成功后可读到 seq 与下游状态；下游 `failed` 时 CLI 报错。
- [ ] runbook 可从零完成一次 handoff，无需试错 base_url。