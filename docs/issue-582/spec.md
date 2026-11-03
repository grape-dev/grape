# Spec — Issue #582: atom_selfcheck run_case 非路径 env 透传

> 基于前序 Issue Agent Spec（评论 id 197187611）落仓。目标分支：weekly-08-mys。

## Objective
修正 `scripts/atom_selfcheck.py` `run_case` 对 `extra_env` 的相对路径锚定启发式：仅路径型 env 键（如 `GRAPE_SRC_DIR`）做仓根锚定，非路径型标量 env（如 `GITCODE_OWNER=stub`）原样透传，消除自检误报绿。

## 现状与约束
- 基线 `weekly-08-mys`（及全仓历史/分支）中 `scripts/atom_selfcheck.py` 未物化（技能包脚本未随仓分发，DEVOPS_SKILLS_DIR 缺失已在 #63 类反馈独立追踪）。
- 本实现按 Spec 锚定契约**创建**该脚本：白名单优先 + `os.path.exists` 探测兜底，从源头规避过宽启发式。

## 锚定契约（In Scope）
1. `PATH_ENV_KEYS` 白名单（初始 `GRAPE_SRC_DIR`）的相对值 → 拼仓根（normpath）。
2. 非白名单键：值为相对路径且在仓根下真实存在（探测兜底）→ 锚定；否则原样透传。
3. URL（含 `://`）、绝对路径、非字符串值 → 一律透传。
4. 契约在模块 docstring + `anchor_env` docstring 标注，扩展方式=向 `PATH_ENV_KEYS` 加键。

## Out of Scope
- DEVOPS_SKILLS_DIR 缺失、技能包物化（独立追踪）。
- atom_selfcheck 其它与锚定无关的逻辑。

## Acceptance Criteria
- [x] 非路径 env（GITCODE_OWNER=stub）原值透传（单元+端到端）
- [x] GRAPE_SRC_DIR 相对值锚定仓根
- [x] 锚定规则在脚本文档/注释标注
- [x] 新增回归用例（tests/atomic/test_issue_582_env_anchoring.py，12 例）
- [x] 现有测试通过
