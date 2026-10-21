# Spec: Issue #392 — 技能上传校验放宽（description 2048 + .mjs 文件类型）

> Branch: `feat/issue-392` (base `weekly-08-ljt`) | Spec source: Issue #392 评论

## Objective
放宽自定义技能上传校验：① description 字段长度上限 500→2048 字；② 附件文件类型白名单增加 `.mjs`（Node ES Module 脚本）。

**背景**：architecture-reviewer 技能包 scripts/*.mjs（4 个脚本）被拒、description 超 500 字被拦，导致完整技能包无法上传。

## Scope (In)
- `src/user/validator.py`: `SCRIPT_EXTS` 增加 `.mjs`（第 14 行）；description 阈值 500→2048 + 错误提示同步（第 90-91 行）
- `tests/test_skills_obs.py`: 新增 `.mjs` 放行用例 + description 边界用例（≤2048 通过 / 2049 报错）

## Out of Scope
- 不调整 `FORBIDDEN_EXTS`（二进制可执行仍禁止）
- 不调整其他字段校验规则（name/version 等）
- 不修改前端上传 UI 文案

## Technical Approach
校验器 `validate_skill()` 中调整两处：
1. `SCRIPT_EXTS` 集合加 `.mjs`（与 `.js` 同类文本脚本，ES Module 格式）
2. description 长度判断 `>500` 改 `>2048`，提示文案同步

## Acceptance Criteria
- [x] architecture-reviewer 技能包重新上传成功（validate_skill 返回空 errors，4 个 .mjs 放行，desc 629 字通过）
- [x] description ≤2048 通过、>2048 报错（测试覆盖 500/2048 通过 + 2049 报错）
- [x] .mjs 不再被拒、二进制可执行仍被拒（FORBIDDEN_EXTS 不变）
- [x] 所有现有测试通过（pre-existing 7 个 _skill_id 相对路径直连失败与本次改动无关）+ 新增测试覆盖
- [x] 目标分支: weekly-08-ljt
