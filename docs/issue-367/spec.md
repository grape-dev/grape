# Spec: Issue #367 — 同名 skill 解析 scope 优先级缺陷修复

> 来源：Issue Agent Spec Document（评论 id=194458530），本文件为开发侧落盘副本。

## Objective
修复技能按名解析的 scope 优先级缺陷：当前同名 skill 一律先查 official
（`src/user/db.py resolve_skill_id` 查询顺序 official→repo→user；
`src/prompt_builder.py:831` 仅当官方不存在才解析 skill_id 个人路径），
个人版在事件链路永远不生效。

## 解析策略（关键决策）
**个人优先**：user（按 user_id，owner_id 强匹配）→ repo（按 repo_key）→ official。
- user_id 缺失（PR 订阅、默认会话重建等无用户上下文链路）→ repo→official 降级，等同现状；
- 用户 A 不误命中用户 B 的个人 skill（owner_id 强匹配）；
- 不改 `resolve_official_skill_path` 官方布局；不做前端显式选择 UI。

## 改动点
1. `src/user/db.py resolve_skill_id()`：查询顺序 user→repo→official，命中处记录 scope 日志；
2. `src/prompt_builder.py build_general_chat_prompt()`：skill 路径注入按 skill_id 记录的
   scope 决定——user/repo scope 一律按 skill_id 解析真实绝对路径，不回退 `{{SKILL_DIR}}`
   官方占位；scope 查询失败时保留旧回退（官方不存在才解析）；
3. 事件链路调用方核对（event_pipeline.py / webhook.py / scheduler.py / runtime.py /
   scenes.py / session_manager.py 均已传 user_id；session_store.py 默认会话重建与
   PR 订阅无用户上下文，按降级语义保持不动）；
4. 新增同名冲突单元测试（tests/test_issue_367_skill_scope_priority.py）。

## Acceptance Criteria
- official 与个人同名并存 → 命中个人版（user>repo>official）；
- 仅 official 时零回归；用户 A 不误命中用户 B 的个人 skill；
- chat prompt 注入：skill_id 指向 user/repo scope 时为真实绝对路径；
- 现有测试全过 + 新增同名冲突用例。
