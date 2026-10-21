# Plan: Issue #406 — VOD skill 显示名退化修复

## 目标
禁用 SRE 后 VOD skill 显示名保持「开发者声音分析（CLI版）」；任意 skill 在启用/禁用其他角色后显示名不退化为内部 id；保存 payload 的 `skill` 字段仍为内部 id（不改保存语义）。

## 改动文件
- `src/api/static/js/pages/dashboard.js`（唯一改动文件）

## 改动点（4 处，全部前端纯 JS）

### 1. 编辑模式 selectAgentSkill（`_renderSceneEditMode` 内，约 line 1199）
选中 skill 时同步写 `node.skill_display_name`，来源 `_skillPickCache[agentType][skillName]`；skillName 为空（清除选择）时同步清空 `skill_display_name`。

### 2. 创建模式 selectAgentSkill（`_enterCreateStandardMode` 内，约 line 1789）
同 #1（两处实现独立，需分别修复）。

### 3. toggleAgentInScene（两处：编辑模式 line 1189、创建模式 line 1780）
禁用 agent 时 `node.skill = ''` 同步补 `node.skill_display_name = ''`（与 skill 同清，保持字段一致性；重新启用后显示「选择技能」占位，与现有产品语义一致）。

### 4. `_renderSceneEditMode` editNodes 构建（约 line 1149-1170）
为每个 editNode 补 `skill_display_name: node.skill_display_name || ag.skill_display_name || ''`，避免编辑模式首次打开已有场景时 skill 显示就退化为内部 id（同根因的另一面）。

## 不改动（Out of Scope）
- 后端 `/scenes/skills/{agent}` API（已返回正确 display_name）。
- `_renderEditFlowCards`（line 1308-1310 现有表达式已优先取 `node.skill_display_name`，注入数据即生效）。
- `_openSkillDropdown`（已正确缓存 `_skillPickCache` + 临时更新 `btn.textContent`）。
- 保存 payload：`_saveSceneEdit` / `_saveCreateScene` 仍提交 `skill: n.skill`（内部 id），不增字段。
- `agents.js` 的 `.cfg-skill` 组件（另一套独立组件）。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 显示名修复位置 | 选中时写入 node（数据模型补字段） | 渲染时查缓存依赖缓存时效（scope 切换会重建），数据模型自带字段最稳；编辑模式已有 skill_display_name 语义，对齐现有约定 |
| 清空行为 | 禁用 agent 时与 skill 同清 | 禁用即放弃该 agent 选择，重新启用后显示「选择技能」占位，与现有产品语义一致 |
| 编辑模式回显 | editNodes 构建时继承 ag.skill_display_name | 避免编辑模式首次打开已有场景时 skill 显示就退化（同根因的另一面） |

## 测试策略
1. **Node JS 纯函数测试**（`tests/js/test_issue_406_skill_display_name.js`）：提取 `selectAgentSkill` 纯函数（editNodes + cache 输入），验证选中/清除/未命中缓存等场景的 `skill_display_name` 写入。
2. **Python 源码静态断言测试**（`tests/test_issue_406_skill_display_name.py`）：断言 dashboard.js 中两处 `selectAgentSkill` 均写 `skill_display_name`、`_renderSceneEditMode` editNodes 构建含 `skill_display_name`、`_renderEditFlowCards` 优先取 `skill_display_name`、保存 payload 不增字段、JS 无语法错误。
3. **回归**：跑现有 dashboard.js 相关测试（test_issue307、test_issue_227、test_pipeline_issue182、tests/js/test_issue_292_skill_dropdown.js、tests/js/test_issue_335_skill_search.js）。

## 风险与缓解
| Risk | Impact | Mitigation |
|------|--------|------------|
| `_skillPickCache` 未命中（下拉未打开过即回显） | Low | 编辑模式回退链 `ag.skill_display_name` 保留；创建模式无回显场景（skill 为空显示占位） |
| 清除选择（skillName=''）走同一 selectAgentSkill | Low | skillName 为空时同步清空 skill_display_name，保持字段一致 |
