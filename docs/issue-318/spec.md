# Spec: Issue #318 — 修复 PR 订阅同一条 Merge Request Hook 双重派发

> 目标分支: mys-local | 方案: B（1.5 预匹配仅 bot 预派发 + 主路径兜底去重）

## 问题根因

`src/api/event_pipeline.py` 的 `run_pipeline` 函数对 PR 订阅事件存在两处派发：

1. **1.5 预匹配**（1112-1156 行）：在自循环检测之前，对 `PR_EVENT_TYPES` 事件匹配场景并 `_dispatch_pr_subscription_extra` 派发所有匹配场景。此阶段对所有 actor（bot 和人类）都执行。
2. **主路由 PR 订阅**（1340-1368 行）：自循环检测之后，当 `agent_type` 为空时重新匹配 PR 订阅，取第一个匹配作为主 `agent_type = "qa"`。

对于**人类用户**（非 bot actor）：
- 1.5 预匹配派发了所有匹配场景（包括第一个）
- 自循环检测未拦截（非 bot）→ 继续主路由
- 主路由再次匹配并派发第一个场景作为主 agent
- **结果**：同一场景被派发两次（seq 成对，如 10846/10847）

对于 **bot actor**：
- 1.5 预匹配派发了所有匹配场景
- 自循环检测拦截（bot）→ `return result`，主路由不执行
- **结果**：单次派发（正确，这是 1.5 预匹配的设计初衷——绕过自循环检测让 bot PR 也能触发 QA）

## 修复方案（方案 B）

### 改动 1：1.5 预匹配仅 actor 为 bot 时执行

在 1.5 预匹配入口处提前解析 actor 并判定是否为 bot：
- actor 为 bot → 维持现有预派发逻辑，并标记已派发的场景 ID 集合到 `evt["_pr_sub_early_dispatched"]`
- actor 非 bot → 跳过预派发（人类用户只走主路由）

### 改动 2：主路由兜底去重

主路由 `_pr_sub_matches` 阶段：检测到 `evt["_pr_sub_early_dispatched"]` 已含该场景则跳过（防御性去重，兜底 bot actor 未被自循环检测拦截的边缘情况）。

### 改动 3：单次派发明确日志

预派发跳过时输出 `Pipeline: PR subscription pre-match skipped (actor=xxx not bot, deferred to main route)`，主路由派发时输出已有日志。

## 影响范围

改动封闭于 `src/api/event_pipeline.py` 的 `run_pipeline` 函数内部，无跨模块新增调用。

## 验收标准

- [ ] 人类用户更新订阅 PR，同一场景仅产生一条事件
- [ ] bot PR 仍触发订阅 QA（不回归 f06732b14）
- [ ] 成对 seq 消失
- [ ] 现有测试通过 + 新增 bot/非 bot 分支测试
