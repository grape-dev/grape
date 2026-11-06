# Issue #978 方案设计与任务拆解

## 方案
前端以「唯一键」贯穿下拉选中/缓存/回显（改动集中 `src/api/static/js/pages/dashboard.js`）：

1. 新增纯函数 `_skillKeyOf(s)`：`skill_id` 优先（`'id:' + skill_id`），缺失回退 `(scope, skill_name)` 复合键（scope 缺省 official）；null 安全。
2. 缓存 `_skillPickCache[agentType]` 改为 `唯一键 -> {name, display}`：同名三档互不覆盖；`_openSkillDropdown` 渲染前构建。
3. 选中高亮：行元素新增 `data-key`；`sel = _skillKeyOf(s) === current`（唯一键精确命中）**或** `current === s.skill_name && _nameCount[skill_name] === 1`（旧裸 name 值仅在名字唯一时回退命中，向后兼容且杜绝同名全勾选）。
4. 点击选中：`selectAgentSkill(agentType, name, key)`（两处实现：编辑模式 + 创建模式），`node.skill` 仍写 skill_name（保存语义不变）；`skill_display_name` 先按 key 命中缓存条目 `{display}`，无 key 按 name 兜底扫描；`data-current` 回写唯一键（重开下拉精确回显）。
5. 保存后回显：`_skillPickCache` 展平为 `name -> display` 映射（pipeline 仅持久化 skill_name，无法进一步区分三档，同名取最后写入，注释说明）。
6. 契约固化：`scenes.py` OpenAPI description + 响应构建处注释声明 `(skill_id, scope, skill_name)` 唯一键、skill_name 不得单独作 key；`help/api/06-scenes.md` 同步记录。

风险与守恒：
- 旧数据（按钮 data-current 为裸 skill_name）经唯一性守卫回退命中，单同名场景行为不变。
- 保存 payload（nodesPayload）显式字段列表不含唯一键字段，后端零改动。
- 搜索/过滤/分组逻辑（`_skillFilterByQuery`/`_skillGroupByScope`）不动，纯选中/缓存/回显路径改造。

## 任务拆解（TDD）
1. RED：新增 `tests/js/test_issue_978_skill_unique_key.js` + `tests/test_issue_978_skill_unique_key.py`（唯一键函数 / 选中单行 / 缓存结构 / 契约注释 / payload 守恒）。
2. GREEN：dashboard.js 五处改造 + scenes.py 契约注释 + help api 文档。
3. 回归：`test_issue_406_skill_display_name`（缓存结构变更，同步适配断言）、`node --check`、`scripts/check-openapi-endpoints.py`。

## 提交
- `fix: #978 use skill_id-based unique key for skill dropdown selection and cache`
