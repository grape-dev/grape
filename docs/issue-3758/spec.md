# Spec: Issue #3758 — 恢复 A2A 目标场景 work_branch 存在性校验与回退

> 基线：`arch-weekly-11-ljt` | 类型：缺陷修复（#1841 失效回归）

## 目标

- A2A 交办时对目标场景配置的 `work_branch` 做存在性校验；分支不存在 → 回退仓库默认分支并留痕。
- 下游 `carried_context.target_branch` 使用回退后的生效分支，`target_branch_source` 保持 `target_scene_work_branch` 权威标记。
- 校验不可核实（无 token / 网络失败 / 分页截断）时保持原配置，不误判回退。
- 导入符号缺失从静默 WARNING 变为可发现的失败（ImportError fail-fast + 导入冒烟测试）。

## 范围

In：
- `src/domains/runtime/engine/repogit.py` 新增公开 `resolve_work_branch(repo_key, configured)`，基于 VCS 分支列表显式比对。
- `src/domains/runtime/routes/webhook.py` 调用点改为模块级导入 + 收窄异常捕获。
- `tests/runtime/test_issue_1753_handoff_target_branch.py` 补「不存在分支 → 回退」「不可核实保持原配置」「导入冒烟」用例。

Out：
- 同族 work_branch 需求侧口径（#3419 / #1936 / #2374）。
- 已 Revert 的 #1841 fold-task A2A contract 内容。

## 验收

- [ ] 配置不存在分支的场景交办后，日志出现 `A2A #1753: target scene work_branch fallback: ...`，下游 `target_branch` 为回退值。
- [ ] 交办链路不再出现 `cannot import name 'resolve_work_branch'`。
- [ ] 多跳 A→B→C（C 无 work_branch）保持 `target_branch_source` 失效为未确认。
- [ ] 分支不可核实时保持原配置。
- [ ] 新增单测覆盖上述路径，缺符号时导入冒烟直接失败。
- [ ] 现有测试全通过。