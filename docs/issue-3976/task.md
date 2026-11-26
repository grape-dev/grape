# Task: Issue #3976 — 技能详情页优化

- [x] 1. 后端具名写查询：`skill_scope_move_update` / `skill_content_hash_update`。
- [x] 2. `POST /by-id/{id}/convert`：原地改 scope（复制对象 → 改 DB → 删旧对象，失败回滚）。
- [x] 3. `PUT /by-id/{id}/content`：个人技能保存 SKILL.md，不写版本行。
- [x] 4. `POST /by-id/{id}/ai-fill`：复用平台 LLM 返回结构化建议。
- [x] 5. 前端：互转按钮、SKILL.md 编辑态、抽屉 AI 填写（只填空字段）。
- [x] 6. i18n（zh/en）与 KEY2_DEFAULTS 兜底文案。
- [x] 7. 新增后端测试 + 运行 skills 相关回归。
- [x] 8. 前端 lint / build 校验。
