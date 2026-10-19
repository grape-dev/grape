# Spec: Issue #292 — 项目场景选择 Agent 技能时显示优化

> 来源：Issue #292 + Issue Agent Spec 评论 | 分支基线：weekly-06-lyz | 纯 UI/交互优化，不改数据结构

## 目标
项目场景（dashboard）编辑页 Agent 技能选择下拉：
1. 按【官方技能 / 仓库技能】分组展示（个人技能存在时追加分组，避免回归丢选项），分组交互参考同页"引擎选择"弹窗（分组标题 + 计数）；
2. 下拉顶部搜索框，按技能名称/描述关键字实时过滤（不区分大小写包含匹配），清空恢复完整列表；
3. 空结果态提示"无匹配技能"。

## 现状
- `_renderEditFlowCards`（dashboard.js）渲染原生 `<select>`，`/scenes/skills/{agent_type}` 返回的技能平铺、无分组、无搜索；
- 引擎选择已有"分组标题 + 搜索框"交互（`_renderEngineList`），可复用其视觉模式。

## 方案
- 新增共享 helper（dashboard.js）：
  - `_skillFilterByQuery(skills, q)`：纯函数，按 skill_name/display_name/description 过滤；
  - `_skillGroupByScope(skills)`：纯函数，按 scope 分组（official → 官方技能、repo → 仓库技能、user → 个人技能，空组不渲染）；
  - `_openSkillDropdown(agentType, btn)`：按钮触发的浮动下拉面板（顶部搜索输入 + 分组列表 + 空态），选中后回调既有 `selectAgentSkill`，不改保存链路；
  - `_skillPickCache`：agentType → {skill_name: display_name}，供保存后刷新显示名。
- `_renderEditFlowCards` / `renderAgentCards` 两处编辑态 `<select>` 替换为触发按钮 + `_openSkillDropdown`；
- `_saveSceneEdit` 的 skillDisplayMap 改为 `_skillPickCache` 展平 + 原有 option 兜底。

## 范围外
技能数据结构 / API 字段变更；技能管理功能；引擎选择等其他组件重构。

## 验收
- [ ] 下拉可见"官方技能 / 仓库技能"分组，归属正确（个人技能存在时单独分组）
- [ ] 搜索实时过滤；清空恢复；无匹配有空态提示
- [ ] 原有技能选择、保存功能回归正常（保存 payload 不变）
- [ ] 新增 node 测试覆盖分组与过滤逻辑（tests/js/test_issue_292_skill_dropdown.js）
