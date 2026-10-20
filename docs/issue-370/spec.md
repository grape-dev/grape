# Spec: Issue #370 — 孤儿回收机制误杀了刚派发的新任务

> 基准分支：`weekly-07-clean-mys` | Spec 源：Issue #370 评论（issue agent 发布）| Developability: ready

## Objective

为 `src/api/routes/runtime.py` 的 `recover_orphans` 增加任务年龄宽限期判断：`started_at`（NULL 兜底 `created_at`）距 now 不足宽限期的任务直接 skipped，防止刚派发、引擎尚未输出首条 `task_messages` 的新任务被误判孤儿反复回炉直至强制 failed。

## Scope

### In Scope
- `src/api/routes/runtime.py` `recover_orphans`：循环内查 `task_messages` 之前增加年龄闸门。
- 宽限期常量：默认 300 秒，环境变量 `RECOVER_GRACE_SECONDS` 覆盖（风格对齐现有 `RECOVER_ACTIVITY_WINDOW`）。
- 单元测试覆盖新逻辑与回归路径。

### Out of Scope
- 回收触发入口（worker 注册、心跳）不改。
- attempts 封顶（3 次）与 force_failed 策略不改。
- 队列出队逻辑、引擎冷启动优化不做。
- 前端/observability 展示不做。

## Technical Approach

在回收判活条件前加一道"任务年龄闸门"：太新的任务不参与孤儿判定。

1. `runtime.py`：读取 `RECOVER_GRACE_SECONDS`（int，默认 300），与现有 `RECOVER_ACTIVITY_WINDOW` 同风格定义。
2. `recover_orphans` 循环内，查 `task_messages` 之前：计算任务年龄基准 `age_base = started_at or created_at`；`now - age_base < grace_seconds` → 记 `skipped_young` 并 `continue`（不回炉、attempts 不变）。
3. 其余逻辑（超龄且无消息 → 回炉 attempts+1；attempts≥3 → force_failed）保持不变。
4. 新增/补充单元测试。

## Affected Modules

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/routes/runtime.py` | 修改 | `recover_orphans` 增加年龄闸门 + `RECOVER_GRACE_SECONDS` 读取；SELECT 扩展 `started_at/created_at` |
| `tests/test_recover_orphans_slots.py` | 修改 | 新增 UT-4/UT-5/UT-6 覆盖宽限期判断与 NULL 兜底；现有 UT-1/UT-2/UT-3 不破坏 |

## Acceptance Criteria

- [ ] 新任务（< 宽限期）触发 recover-orphans → skipped，attempts 不变、不回炉
- [ ] 超龄且无 task_messages 的任务 → 回收回炉，attempts+1（真实孤儿行为保留）
- [ ] 有近期 task_messages 的任务 → skipped（回归现有行为）
- [ ] 老孤儿反复回炉至 attempts=3 → 仍能 force_failed（安全网不失效）
- [ ] `started_at` 为 NULL 时兜底 `created_at`（宁可多保护不误杀）
- [ ] `RECOVER_GRACE_SECONDS` 环境变量可覆盖默认 300s
- [ ] 所有现有测试通过

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 真实孤儿回收延迟一个宽限期（≤5 分钟） | Low | 不影响最终回收；宽限期可通过环境变量调小 |
| `started_at` 为 NULL 的存量数据 | Low | 兜底用 `created_at`，宁可多保护不误杀 |
| 时间戳格式异常 | Low | 解析失败保守跳过闸门，继续后续活跃度检查（保持回收行为） |
