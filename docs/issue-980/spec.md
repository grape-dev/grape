# Issue #980 Spec — 技能显示名按实际绑定档位解析

## 目标
修复技能显示名映射只查 `scope='official'` 导致三档同名技能（official/repo/user）在场景列表
（`src/api/routes/scenes.py` with-pipeline）与 dashboard（`src/api/routes/dashboard.py`）被显示成官方名。

## 范围
- 新增共用解析层 `src/api/skill_display.py`：
  - `resolve_skill_display_name(skill_name, skill_id, scope, owner_id, repo_key, user_id)`
  - `build_skill_display_map(repo_key, user_id)`（批量版，避免 N+1）
- scenes.py 与 dashboard.py 两处调用点替换 official-only 映射。
- 单测覆盖三档同名区分、skill_id 缺失/失活两条回退、无冲突零回归。

## 解析优先级（与派发侧 resolve_skill_id 一致，Issue #367 个人优先）
1. `skill_id` 精确命中（is_active=1）
2. 显式 `(scope, owner_id)` 档位
3. `user` scope（owner_id=user_id）
4. `repo` scope（owner_id=repo_key）
5. `official` 兜底
6. skill_name 本身

失活技能视为未命中，沿回退链继续。

## Out of Scope
- 选择/绑定侧可区分化（#978/#979）。
- schema / API 契约变更（`skill_display_name` 字段语义不变，仅取值来源修正）。

## 验收
- 三档同名各自正确显示。
- 无冲突时与旧 official-only 行为逐字一致（零回归）。
- 两条回退路径（skill_id 缺失、skill_id 失活）有单测。
