# Issue #2890 方案设计：守卫异常聚合

## 方案概述

在 `branch_guard` 模块内引入 `contextvars.ContextVar` 形式的异常收集器：
`hook_main` 进入时创建列表并绑定，`_hook_main` 返回后解绑；各环节异常通过
`_guard_stage_failed(stage, message)` 上报。

- 收集器在场（pre-push 钩子）→ 异常入列 + `log.debug` 明细，不直接 `log.warning`；
- 收集器不存在（服务端 `preflight_push` 等）→ 保持原 `log.warning` 行为，不影响排障。

`hook_main` 在 `rc == 0` 且存在异常时调用 `_emit_fail_open_alert`，向 stderr 写入
唯一一条 JSON：
`{"level":"WARNING","component":"branch_guard","event":"branch_guard.fail_open",
"reason":..., "repo_key":..., "attempts":[{"stage","error"}...], "fail_open":true}`。

真实拦截（`rc == 1`）已有独立阻断提示，不再附加 fail-open 聚合告警；顶层兜底异常
（`_fail_open`）会先清空已收集项，避免与既有唯一告警重复。

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 聚合点 | 各环节自行汇总 / hook_main 统一聚合 | hook_main 统一聚合 | 单一出口，天然保证「一条」 |
| 传递方式 | 参数透传 / 模块级全局 / ContextVar | ContextVar | 不侵入既有函数签名，线程/上下文安全 |
| 服务端日志 | 全部降 debug / 按收集器分流 | 按收集器分流 | 钩子单条化，服务端保留可观测性 |
| 聚合输出 | 直接写 / try 包裹 | try 包裹 | fail-open 优先，聚合自身异常不阻断 push |

## 影响范围

- `src/domains/runtime/engine/branch_guard.py`：新增收集器与聚合输出，改写 6 处单点告警。
- `tests/test_issue_2890_branch_guard_aggregate_warning.py`：新增缺表场景回归。

## 风险

- 聚合输出抛错：try/except 静默兜底，push 不受影响。
- 单点明细被弱化：保留 debug 级 + 聚合 `attempts`，可定位到具体阶段与错误。