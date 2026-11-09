# Plan: Issue #561

> 端点形态：独立 by-id 更新端点（B）；定位方式：按 skill_id（B）；写序：留存旧对象字节可回滚（对齐 #444）。

## 实现步骤
1. 后端 `src/api/routes/skills.py` 新增 `update_skill_by_id`：
   - 查行（id/skill_name/scope/owner_id/agent_type/category_id）→ 404/403（official、user 非本人、repo 非托管）。
   - 复用 upload 的 zip 校验（10MB / 炸弹 / validate_custom_skill / frontmatter description）。
   - zip 技能目录名必须等于既有 skill_name（否则 400，防止 upsert 落到新行）。
   - agent_type/category_id 可选（缺省沿用原值）；校验合法性与分类存在性。
   - 写序：prev_bytes → save_bytes → upsert_skill_obs（按 scope/owner/name 命中 UPDATE 原行，保留 id/引用/点赞）→ 失败回滚 + 409 → zip_cache_put + mem_cache_invalidate。
2. 前端 `skills.js`：卡片 actionBtns 增加「上传更新」按钮（scope != official）；`showSkillUpdate`/`doSkillUpdate` 弹窗上传，成功后 renderSkills()。
3. 测试 `tests/test_issue561_skill_update.py`：临时 SQLite + 内存存储（对齐 test_issue444 模式），直调 async 路由函数。

## 风险
- upsert 按 (name, scope, owner) 匹配 → 名称不匹配时拒绝而非新建（400 明确文案）。
- 并发覆盖 → 沿用 #444 最后写者胜 + 409 回滚。
