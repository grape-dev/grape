# Issue #4260 任务拆解

- [x] T1 后端哨兵：`skills.py` `get_skill_detail_by_id` 三哨兵 → 400，置于名称解析前
- [x] T2 后端空路径：`GET /api/skills/by-id/` → 400 `invalid_skill_id`
- [x] T3 前端守卫：`nav()` skill 分支缺参 → 提示 + `nav('skills')` + return
- [x] T4 前端 hash：`_pushHistory` skill 缺参改写 `#skills`
- [x] T5 前端文案：skill catch 按 400/404 分流，新增 `common.invalid_param`（zh/en、内联+JSON）
- [x] T6 缓存版本：`index.html` app.js v311 / `__I18N_CACHE_VER__` 91
- [x] T7 回归：后端 `test_issue4260_skill_id_sentinel.py`、前端 `test_issue_4260_skill_missing_param.js`
- [x] T8 自审查：5 轴审查 + 开源前置增量检查（敏感扫描/无关文件拦截）