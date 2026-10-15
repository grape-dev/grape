# Spec: Issue #336 — skill上传时校验的version字段条件放宽

> Coding Agent | 基于 Issue Agent 已发布 Spec 执行 | 目标分支: local-liufei | 开发分支: feat/issue-336

## Objective
放宽 skill 上传时 SKILL.md Front Matter `version` 字段的格式校验：`1`、`1.2`、`1.2.3` 及 `v` 前缀（如 `v1.2.3`）均可通过；非法格式（`abc` / `1.2.3.4` / `1.x`）仍返回 400。

## Scope

### In Scope
- 后端 `src/user/validator.py` `validate_skill()` 中 version 校验正则放宽（单处修改）+ 报错文案同步。
- `tests/test_skills_obs.py` 补充放宽后格式单测用例。

### Out of Scope
- 前端 `src/api/static/js/pages/skills.js`（无同源校验，仅透传后端 errors）。
- 存储 v2.4（已去版本化，object key 不含 version）。
- version 缺失时的默认值逻辑（保持默认 1.0.0 现状）。

## Technical Approach
- 现状正则：`^v?\d+\.\d+(\.\d+)?$`（至少两段数字，`1` 被拒）。
- 目标正则：`^v?\d+(\.\d+){0,2}$`（1~3 段数字，`v` 前缀兼容）。
- 校验调用链（已核实，全仓库唯一下游）：`src/api/routes/skills.py:7` 导入 `validate_skill as validate_custom_skill`，`upload_skill()`（skills.py:711）调用 → 400 errors 数组返回；repo scope 上传与 user scope 走同一 `validate_skill()`，单处修改即覆盖两种 scope。

## Acceptance Criteria
- [x] SKILL.md `version: 1` 上传成功（user 与 repo 两种 scope 均验证）
- [x] `1.2`、`1.2.3`、`v1.2.3` 通过；缺失 version 默认 1.0.0 不报错
- [x] 非法格式（`abc` / `1.2.3.4` / `1.x` / `1.` 等）仍 400 且 errors 含明确提示
- [x] 既有上传链路测试（tests/test_skills_obs.py 等）无回归
- [x] 新增针对放宽后格式的单测用例并通过
