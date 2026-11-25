# Plan: Issue #4185 — 目标分支变化纳入 PR 订阅触发门槛

## 方案

1. 新增 `_pr_target_branch_changed(payload: dict) -> bool`：
   - 读取规范化后的 `payload["_changes"]`，回退原始 `payload["changes"]`；
   - 仅当存在 `target_branch` 变更条目时继续判定；
   - 支持 `previous/current`、`old/new`、`from/to` 前后值形态；
   - 前后值齐全且相等 → 未变化；其余「字段出现即变更」→ 变化；
   - 字段缺失 → 未变化（保持旧行为）。
2. 早期 1.5 预匹配：`action=update` 时触发条件由「有新 commit」放宽为「有新 commit 或目标分支变化」；否则继续 `no_commit_on_pr_update` 跳过。
3. 主路由 PR 订阅：同一口径放宽；`merged` 分支不变。
4. 出站台账：`pull_request_updated` 的放行条件同样纳入目标分支变化，避免人工改目标分支被系统回声台账拦截。
5. 匹配本体 `_match_pr_subscription` 不改动，仍按新目标分支与场景 `work_branch` 精确匹配。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 触发判定 | 新 commit 或目标分支变化 | 与「按新目标分支重新匹配」需求和新建 PR 口径一致 |
| 防重复 | 分支未变且无 commit 才跳过 | 同一更新不重复触发 |
| 匹配逻辑 | 复用现有函数 | 根因是门槛缺失而非匹配失败 |
| 台账放行 | 目标分支变化视同新 commit | 人工改目标分支不是系统回声，否则订阅匹配到不了 |

## 影响面

- `event_pipeline.py` PR 更新处理与 PR 订阅匹配前置门槛。
- 回归测试新增于 `tests/runtime/test_issue_4185_pr_target_branch_change.py`。
- 既有 PR 订阅、台账回放测试需保持通过。

## 风险

- 事件字段缺失导致误判：缺失或前后相等按未变化处理。
- 平台 payload 形态差异：兼容多种 changes 前后值键。
- `merged` 误触发：merge 分支先短路，行为不变。
