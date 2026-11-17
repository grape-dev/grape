# Issue #2890 Spec：pre-push 分支守卫缺表时聚合单条结构化告警

> 目标分支：`weekly-mys` | 优先级：P2 | 类型：bug

## 目标

`grape git push` 的 pre-push 分支守卫（`src/domains/runtime/engine/branch_guard.py`）
在守卫自身异常（典型：会话库缺 `branch_guard_state` 表）时，单次推送 stderr 只输出
**一条**聚合的结构化 WARNING JSON，并显式标记 `fail_open=true`，避免上层 Agent 把
5 条独立 WARNING 误判为推送失败而重试。

## 现状

`load_lease_state` / `save_lease_state`（被 `reconcile_pending`、`record_pending_push`
复用）/ `fetch_pr_state` / `_fetch_pr_state_via_grape` 各自独立 `except Exception` 后
`log.warning`。缺表场景一次 push 会输出 state read ×3、PR query、state write 共 5 条
WARNING JSON，最终虽 fail-open 放行，却没有统一的「已放行」结论行。

## 范围

### In Scope

- 钩子作用域内收集各环节守卫异常，`hook_main` 结束时统一输出唯一一条结构化告警
  （`attempts` 异常汇总、`reason` 汇总原因、`fail_open=true`）。
- 聚合输出自身 try/except 兜底，序列化/stderr 异常不阻断 push。
- 单点异常在钩子收集器在场时降为 debug（保留排障明细）；服务端路径保持原 warning。
- 新增单测覆盖缺 `branch_guard_state` 表场景：stderr 告警 JSON 仅 1 条、退出码 0。

### Out of Scope

- 守卫 fail-open 策略本身（#2186 / #2348 已落地，不回归、不改动）。
- PR #1540 的会话库补表方案。

## 验收标准

- [ ] 缺 `branch_guard_state` 表时，单次 hook 运行 stderr 守卫告警 JSON 仅 1 条，含 `fail_open=true`
- [ ] 单点异常（state read / PR query / state write）降 debug 或去重
- [ ] 新增单测断言 stderr 仅 1 条告警、hook 退出码 0
- [ ] fail-open 放行行为不回归（缺表不阻断 push）
- [ ] 现有相关测试全通过