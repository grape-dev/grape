# Spec: Issue #4185 — PR 目标分支变更后 PR 订阅 QA 流水线未触发

## 背景

`pull_request_updated`（`action=update`）路径以「存在新 commit」作为 PR 订阅匹配的唯一前置门槛。
仅手动修改 PR 目标分支、没有新 commit 时，事件被判为 `no_commit_on_pr_update` 直接跳过，
导致订阅了新目标分支（如 `arch-weekly-11`）的 QA/架构守护流水线不触发。

## 目标

- PR 目标分支变化（即使无新 commit）时，按新的目标分支重新执行 PR 订阅匹配。
- 复用既有 `_match_pr_subscription`，不改动匹配算法（按 `work_branch` 精确相等）。
- 目标分支未变且无新 commit 时继续跳过，避免重复触发。
- `merged` 行为保持不变。

## 非目标

- 不新增订阅配置项或前端交互。
- 不修改按 `work_branch` 匹配订阅场景的本体逻辑。
- 不改变无关目标分支的匹配结果（不误触发）。

## 实现范围

- `src/domains/runtime/pipeline/event_pipeline.py`
  - 早期 1.5 预匹配（bot actor 预派发）触发门槛。
  - 主路由 PR 订阅触发门槛。
  - 出站回帖台账放行判定：目标分支变化属于人工动作（非系统回声），与「有新 commit」同等放行，避免台账在订阅匹配前拦截。
- 新增目标分支变化判定工具函数，兼容 `changes.target_branch` 的 `previous/current`、`old/new`、`from/to` 形态，并保留 `object_attributes.target_branch` 兜底。
- 新增回归用例四类：目标分支切换（无新 commit）、分支未变无 commit、`merged`、跨流水线移交。

## 验收标准

- [ ] 目标分支 `arch-weekly-11-zj` → `arch-weekly-11`（无新 commit）时，`work_branch=arch-weekly-11` 的订阅 QA 流水线被触发。
- [ ] 无关目标分支不误触发。
- [ ] `merged` 事件仍不触发 PR 订阅。
- [ ] 目标分支未变化且无新 commit 的更新不重复触发。
- [ ] 跨流水线移交后，目标场景按自身 `work_branch` 的匹配规则一致生效。
- [ ] 现有测试通过。