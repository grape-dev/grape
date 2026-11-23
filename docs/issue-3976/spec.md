# Spec: Issue #3976 — 技能详情页优化

> Coding Agent | 2026-11-23 | 目标分支 arch-weekly-11-ldw-02

## 需求

在「【场景·技能】→ 技能 → 详情」（`SkillsView.vue`）补齐三项能力，官方技能全程只读：

1. 仓库技能 ↔ 个人技能在详情页右上角一键原地互转 scope。
2. 个人技能可在详情页在线编辑 SKILL.md，保存后版本号不递增。
3. 「编辑信息」抽屉新增「AI 填写」，异步识别 SKILL.md 填充摘要/描述/五槽/标签。

## 范围

### In Scope
- `POST /api/skills/by-id/{id}/convert`：原地改 scope（user ↔ repo），保留 id、历史版本行、`scene_skill_ref`、点赞。
- `PUT /api/skills/by-id/{id}/content`：仅个人技能可保存 SKILL.md 正文；不写入版本表（版本号不变）。
- `POST /api/skills/by-id/{id}/ai-fill`：后端读取 SKILL.md，调用平台既有 LLM（沿用 autopilot 的 hermes 配置读取方式），返回结构化建议，不落库。
- 前端：互转按钮随 scope 渲染；SKILL.md 编辑态；抽屉「AI 填写」仅回填空字段，失败保留已填内容。
- 测试：互转原地性、版本不增、官方/仓库 SKILL.md 只读、AI 失败不破坏已填内容。

### Out of Scope
- 官方技能任何写操作；技能包其它文件在线编辑；跨仓库迁移；新增外部模型服务。

## 验收标准
- [x] 仓库技能显示「转为个人技能」，个人技能显示「转为仓库技能」；官方不渲染；转换后 id/历史版本/场景引用不变。
- [x] 官方技能无互转入口，SKILL.md 与信息编辑只读。
- [x] 个人技能保存 SKILL.md 后版本号不递增；官方/仓库技能 403。
- [x] 抽屉「AI 填写」异步填充，失败有提示且不清空/覆盖已填内容。
- [x] 现有测试通过，新增测试覆盖上述行为。