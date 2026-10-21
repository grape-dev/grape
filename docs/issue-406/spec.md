# Spec: Issue #406 — 添加场景：VOD skill 选「开发者声音分析（CLI版）」后禁用 SRE，skill 显示名错误变为 vod-cli

> 本地副本；权威 Spec 已由 Issue Agent 发布到 Issue #406 评论（comment id 195000627）。
> 此处仅保留指针，开发以 Issue 评论中的 Spec 为准。

## 摘要
- Bug：项目场景 → 添加场景（创建模式），VOD skill 选「开发者声音分析（CLI版）」（skill_name=vod-cli）后禁用 SRE，VOD 卡片 skill 显示退化为内部 id `vod-cli`。
- 根因：`selectAgentSkill` 只写 `node.skill`（内部 id），editNodes 无 `skill_display_name`；`toggleAgentInScene` 禁用 SRE 触发 `_renderEditFlowCards` 整卡片区重渲染，`skillDisplay` 回退渲染 `node.skill` → 内部 id。
- 修复：`selectAgentSkill` 选中时同步补写 `node.skill_display_name`（取自 `_skillPickCache[agentType][skillName]`）；`_renderSceneEditMode` 构建 editNodes 时继承 `skill_display_name`；`toggleAgentInScene` 禁用时与 `skill` 同清 `skill_display_name`。不改后端、不改保存语义。
