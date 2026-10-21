# Task: Issue #406 — VOD skill 显示名退化修复

## 任务拆解（原子提交）
1. **fix(#406): selectAgentSkill 补写 skill_display_name（编辑+创建双模式）**
   - dashboard.js line ~1199（编辑模式）+ line ~1789（创建模式）
   - 选中时 `node.skill_display_name = _skillPickCache[agentType][skillName] || ''`
   - skillName 为空时同步清空
2. **fix(#406): toggleAgentInScene 禁用时与 skill 同清 skill_display_name**
   - dashboard.js line ~1189（编辑模式）+ line ~1780（创建模式）
   - `node.skill = ''; node.skill_display_name = ''; node.engine_runtime_ids = [];`
3. **fix(#406): _renderSceneEditMode editNodes 构建 inherit skill_display_name**
   - dashboard.js line ~1149-1170
   - `skill_display_name: node.skill_display_name || ag.skill_display_name || ''`
4. **test(#406): 前端纯函数测试 + 源码静态断言**
   - tests/js/test_issue_406_skill_display_name.js
   - tests/test_issue_406_skill_display_name.py

## 验收清单（同 Spec）
- [ ] 创建模式：VOD 选「开发者声音分析（CLI版）」→ 禁用 SRE → VOD skill 按钮仍显示「开发者声音分析（CLI版）」
- [ ] 创建模式：任意 agent 的任意已选 skill，在禁用/启用其他角色后显示名不退化为内部 id
- [ ] 编辑模式：同样联动操作后显示名不退化（含保存后刷新回显）
- [ ] 禁用某 agent 后其 skill 按钮仍显示「已停用」占位（不回归现有行为）
- [ ] 保存 payload 的 `skill` 字段仍为 skill_name 内部 id（不改变保存语义）
- [ ] 所有现有测试通过；JS 无语法错误
