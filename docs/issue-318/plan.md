# Plan: Issue #318 — 实现方案

## 文件改动

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | `run_pipeline` 1.5 预匹配条件收紧 + 主路由去重兜底 |
| `tests/test_pr_subscription_dedup.py` | 新增 bot/非 bot 分支测试 |

## 实现步骤

### 1. 提前解析 actor（1.5 预匹配前）

将 `actor = resolve_actor(...)` 从 1159 行提前到 1.5 预匹配之前（1112 行之前），并提前查询 bot 用户名列表判定 `_is_bot_actor`。

### 2. 1.5 预匹配条件收紧

在 `if _pr_sub_matches_early:` 分支内增加 `_is_bot_actor` 判定：
- bot → 派发并记录 `evt["_pr_sub_early_dispatched"]`（set of scene_id）
- 非 bot → 跳过，输出 deferred 日志

### 3. 主路由兜底去重

在主路由 `if not agent_type and event_type in PR_EVENT_TYPES:` 分支内，`_filter_pr_sub_matches` 之后增加去重过滤：排除 `evt["_pr_sub_early_dispatched"]` 中已派发的场景。

### 4. 日志

- 预匹配跳过：`Pipeline: PR subscription pre-match skipped (actor=xxx not bot, deferred to main route)`
- 预匹配派发：保持现有 `Pipeline: PR subscription pre-match -> scenes=[...]`
- 主路由派发：保持现有 `Pipeline: PR subscription triggered -> scene=xxx`

## 测试策略

- **RED**：先写测试验证非 bot actor 场景下 `_dispatch_pr_subscription_extra` 仅被调用 0 次（1.5 不预派发）+ 主路由派发 1 次
- **GREEN**：实现修复使测试通过
- **bot 回归**：验证 bot actor 仍走 1.5 预派发路径
