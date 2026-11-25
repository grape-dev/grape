# Issue #4186 方案设计（plan）

## 根因

worker 侧 `agent.py _execute_task` 把 `{{SKILL_DIR}}` 替换为 `cwd/.skills`（cwd = 任务路由命名的工作区），但任务行 prompt 可能已携带**另一来源**构造的技能路径（按 agent 类型命名，如 `issues/<n>/issue/.skills`——direct 端/历史链路提前烧进 prompt 的字面路径）。两处构造不同源且无存在性探测 → 提示路径可能指向从未物化的目录（反馈 1304：提示 `issue/.skills`、实际 `sre/.skills`）。

## 修复口径（与 Spec「Technical Approach」一致）

落点：worker 技能物化/路径注入模块（`src/domains/runtime/engine/agent.py`）。

物化完成后新增一步**同源化路径规整** `_normalize_prompt_skill_path`（可单测）：

1. **单一来源**：权威可读目录由实际物化结果解析——`_resolve_prompt_skill_root`：
   `DEVOPS_SKILLS_DIR` 下 `<skill>/SKILL.md` 可读（优先）> `cwd/.skills` 下可读；均不可读 → degraded。
2. **错配重写（aligned）**：prompt 中 `Skill file:` / `same directory as` 引用的目录若不可读而权威目录可读（路由名 ≠ agent 类型形态），把两处路径重写为权威路径，并追加 `[Skill Materialization Notice] ... (aligned)` 说明。
3. **降级标注（degraded）**：无任何可读 SKILL.md 时追加 `(degraded)` 说明，给出回退路径（DEVOPS_SKILLS_DIR/cwd 物化目录 + `grape skills detail`）。
4. **幂等**：引用与权威一致且可读 → 零改动（正常任务不注入噪声）。

不引入新配置项；不改 direct 端（其替换与物化本就同 ws 同源）。

## 关键决策

- 注入优先级：DEVOPS_SKILLS_DIR 可读路径 > cwd 物化目录 > 显式 degraded 标注。
- 只重写引用根目录等于不可读引用的 `Skill file:` 行（不动多技能共存的其它可读行）。
- 引用路径本身可读时不重写（防误伤历史会话内其它技能路径）。

## 回归

- `tests/mgmt/test_skill_materialize.py`（#564 原子物化/官方回退）、`tests/runtime/test_os_adapter.py` 全绿。
- `test_skill_fallback_logging.py` / `test_issue887_skill_consistency.py` / `test_issue1635_skill_baseline_drift.py` / `test_issue1070_skill_cross_skill_refs.py` / `test_runtime_hermes.py` / `test_runtime_codex.py` 与基线结果一致（基线既有 5+2 失败，非本单引入）。

## 与 #3663 关系

同落点簇（worker 技能物化/路径注入），本单改动为纯增量（新增物化后规整步），不修改既有物化/回退逻辑，可独立验收；#3663 合入时按其自身验收回归即可。
