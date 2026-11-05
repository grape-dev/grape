# Issue #978 Spec — 场景编辑技能下拉三档同名技能可区分

## 目标
场景编辑技能下拉中 official/repo/user 三档同名技能互不干扰：仅真正选中的行高亮，回显准确，契约固化唯一键。

## 现状（根因）
- 选中判断 `var sel = s.skill_name === current;`（`src/api/static/js/pages/dashboard.js`）——同名三行同时勾选。
- 显示名缓存 `cache[s.skill_name] = ...` 与回显写回 `node.skill_display_name = cache[skillName]`——同名互相覆盖。
- 后端 `GET /scenes/skills/{agent_type}` 已返回 `skill_id / skill_name / display_name / scope / owner_id`（`src/api/routes/scenes.py`），前端丢弃了 skill_id/scope。

## 期望行为
1. 选中/缓存 key 改为 `skill_id`（缺失时回退 `(scope, skill_name)` 复合 key）；禁止再用裸 skill_name 作 key。
2. 选中高亮仅命中唯一 key 对应的一行；同名其他档保持未选。
3. 缓存以唯一 key 存储 display_name，避免同名覆盖导致按钮文案错档。
4. 保存后回显按唯一 key 命中真实条目（pipeline 仅持久化 skill_name，回显按 name 展平映射）。
5. 契约固化：`(skill_id, scope, skill_name)` 唯一键写入 OpenAPI description（scenes.py）+ 前端注释 + help/api/06-scenes.md。

## 验收标准
- [x] official/repo/user 三档同名技能在下拉出现 3 行，分组标签 + display_name 可区分，互不重复
- [x] 选「个人技能」行 → 仅该行勾选；关闭重开下拉仍只勾这一行（data-current 回写唯一键）
- [x] 选「官方技能」行 → 仅官方行勾选，按钮文案与该档 display_name 一致
- [x] 契约文档（OpenAPI description / 前端注释 / help api 文档）明确唯一键含 skill_id（或 scope）
- [x] 保存 payload 不变（仍只提交 skill=skill_name），不改后端保存语义

## 非目标
- 不改 `/scenes/skills` API 响应结构（字段已齐全）
- 不改 skill 表结构（`UNIQUE(skill_name, scope, owner_id)` 允许三档重名共存）
- 不改保存 payload / pipeline 持久化结构（nodes.skill 仍为 skill_name）
