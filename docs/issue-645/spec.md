# Spec: Issue #645 — 技能「上传更新」i18n 键名裸奔防御性加固

> 继承 Issue 评论已发布 Spec（评论 id 197513181，评级 ready）。本文件为开发侧落地版，与 Spec 语义一致。
> 勘误：前序分析称 6 个 i18n 键在目标分支已存在——实际核对 `weekly-08-mys`（HEAD=e50c424bb）发现键缺失，按钮实际显示键名（主缺陷真实存在），故本次同时补键与加 fallback。

## 目标
技能卡片「上传更新」弹窗与更新 toast 在 i18n 键缺失或语言包加载失败时显示兜底文案，绝不显示键名；中英文切换文案正确；弹窗标题技能名与卡片标题口径一致。

## 现状（实测）
- `src/api/static/js/pages/skills.js` 更新流程 6 处使用 `I18N.t(key)||'兜底'` 写法（行 506/876/877/888/916/920）；`I18N.t` 本身支持第三参 `fallback`（`i18n.js:447`），`||` 写法在值为空串/假值时不生效且语义混乱。
- `zh/en skills.json` 均缺 6 个键：`action.update_skill`、`action.confirm_update`、`update.title`、`update.tip`、`toast.update_success`、`toast.update_failed` → 界面显示 `skills.action.update_skill` 等键名。
- `showSkillUpdate(${s.id},'${s.name}')` 传内部名 `s.name`，卡片标题用 `s.display_name || s.name`，口径不一致。

## 方案
1. `skills.js` 6 处 `I18N.t(key)||'兜底'` → `I18N.t(key, null, '兜底')`。
2. `showSkillUpdate` 调用传 `s.display_name || s.name`（含单引号转义）。
3. `zh/en skills.json` 补齐 6 个键（toast.update_failed 不带 `{{msg}}` 占位，因调用点 `e.message ||` 已先取具体错误信息，避免占位符裸奔）。

## 验收
- 6 处 I18N.t 用第三参 fallback；
- `showSkillUpdate` 传 `s.display_name || s.name`；
- zh/en 切换文案正确；
- 模拟键缺失显示 fallback 而非键名。
