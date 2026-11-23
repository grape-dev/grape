# Spec: Issue #3827 — #3800 退避/熔断修复在全网主控生效（legacy id 路径 + error_code 落库兜底）

> 来源：Issue #3827（Spec 由 Issue Agent 发布于评论 202868280）。本文件为 coding 侧实现摘要。

## Objective
让 #3800 的「引擎永久离线 → 指数退避 + 同因熔断」在全部 4 台主控实际生效：task 83 在
引擎持续离线期间 1 小时窗口 ERROR 归零，不再每 60s 新建 exec。

## 关键事实
- #3800 修复（commit `0167a0e6a`）已并入基线 `arch-weekly-11-ljt`，当前 HEAD `c1820641c` 含该修复。
- task 83 绑定的是 **legacy 引擎 id `17877`**，故需证明 `uid 池 / legacy id 池` 两条路径
  在「探测 → error_code 落库 → 退避/熔断 → 结构化日志」整链同构。
- 生产主控仍 60 条/小时空转，最高概率断点是部署覆盖（M1，sre）；代码侧需补齐
  「`error_code` 缺列/旧写入方未落码 → 退避链路被整条断掉」的兜底。

## In Scope（coding）
1. legacy id 池的假时钟 1h 回归用例（0 依赖生产，覆盖探测/退避/熔断/自愈）。
2. `autopilot_executions.error_code` 落库失败（存量库缺列 1054 / 旧写入方未落码）时，
   从本模块产出的稳定指纹回推 `ENGINE_OFFLINE` / `BINDING_STALE`，保证退避链路不断。
3. 结构化日志补 `n`（失败次数）字段，与既有 `task_id` / `backoff_until` / `error_code` 对齐。

## Out of Scope
- 生产主控重启、逐台实查、4 台 host 断言（M1/M4，sre 执行）。
- 引擎离线根因本身（归 #259）；Autopilot 架构重构。

## Acceptance Criteria（coding 可验证部分）
- [ ] 新增假时钟用例覆盖 legacy id 路径：1h exec ≤4、退避 120/240/480、熔断告警 1 条、恢复自愈。
- [ ] `error_code` 为空且 error 文案含环境性指纹时，仍进入退避（新增回归用例）。
- [ ] 结构化日志含 `task_id` / `n` / `backoff_until` / `error_code`；终态 ERROR 带 `error` / `engine_ids`。
- [ ] 现有测试全通过。