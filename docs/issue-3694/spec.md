# Issue #3694 需求文档：删除技能确认弹窗把 `{{name}}` 当字面量直出

> 目标合入分支：`arch-weekly-11-ljt` | 来源：Issue #3694（Spec comment 202752858）| 2026-11-21

## Objective

- 修复技能管理页「删除技能」二次确认弹窗的文案缺陷：用户看到的是模板占位符
  `{{name}}`，而不是被删技能的真实名称；标题与正文还重复了同一句，正文多出一行孤立 🗑。
- 用户价值：不可逆删除前用户能确认「删的到底是哪个技能」，文案可信、可读。

## 现象与根因

1. `src/api/frontend/app/src/views/SkillsView.vue:826-844` 删除确认 `AGModal`：
   - 829 行 `:title="tc2('skills.confirm.delete_skill')"`
   - 834 行 `<h3>{{ tc2('skills.confirm.delete_skill') }}</h3>`
   - 832 行 `🗑` 独立成一行文本
   标题与正文渲染同一句未插值模板，出现两次 `{{name}}`。
2. `tc2()`（903-906）直接返回 `G.I18N.t(key)`，**不做占位符替换** → `{{name}}` 作为普通文本直出。
3. 词条 `shell/locales/zh/skills.json:69` = `确定删除技能 {{name}} 吗？`；
   `shell/locales/en/skills.json:69` = `Are you sure you want to delete skill {{name}}?`（中英双语均泄漏）。
4. 历史扁平键 `confirm.delete_skill`（zh/en:165）为单花括号 `{name}`，当前代码未使用，**不误改**。
5. 可复用范式：`convert` 弹窗用 `td('convert_name').replace('{name}', ...)`；
   shell `app.js:3986-3988` 用 `.replace('{{minutes}}', ...)`；
   被删对象存放在 `deleteCtx.skill`（2496-2500），展示名复用既有 `titleOf(s)`（1124-1126，`display_name || name`）。

## 范围

### In Scope

- `SkillsView.vue` 删除确认弹窗：标题插值、移除正文重复句、长名换行样式。
- `shell/locales/{zh,en}/skills.json`：新增空名降级词条 `confirm.delete_skill_fallback`。
- 语言包 cacheVer 联动（`index/admin/register/forgot` + 指纹 ratchet），新增词条才能被老客户端拉到。
- 新增 `tests/js/test_issue_3694_skill_delete_confirm.js` 源码级回归用例。

### Out of Scope

- 不改 `askDelete` / `confirmDelete` 的调用链与删除接口行为。
- 不改 `AGModal` 组件本体，不改其它弹窗。
- 不改历史扁平键 `confirm.delete_skill`（单花括号）语义。
- 无后端 / 数据变更，不做视觉重构。

## 验收标准

- [ ] 删除任意技能，标题显示真实技能名（zh: `确定删除技能 X 吗？`），界面无 `{{name}}`/`{name}` 残留
- [ ] 名称随被删对象变化（`display_name` 优先，回退 `name`）
- [ ] 标题与正文不再重复同句；正文仅图标位 + `delete_skill_tip`，无孤立 🗑 文本行
- [ ] 空名/全空白 → 降级 `该技能`（en: `this skill`），无空占位、无渲染报错
- [ ] 超长名（中英文 ≥80 字符无空格串）在标题行内换行，不溢出弹窗
- [ ] 取消/确认删除、按钮 loading 文案、删除接口调用与 toast 行为完全不变
- [ ] `node tests/js/test_issue_3694_skill_delete_confirm.js` 通过；`src/api/frontend/app` 的 `npm run lint`、`npm run build` 通过
- [ ] 静态入口/CSS 缓存版本与本次改动联动抬档（`all-views.css`、AGModal 使用入口、入口运行时注入 CSS），
      `pytest tests/api/test_frontend_cache_version_ratchet.py tests/api/test_frontend_bundle_source_ratchet.py` 为绿
- [ ] 技能名含 `$`（`$&`/`$1`）时标题按字面量渲染，不出现模板片段
- [ ] 既有 `tests/js` i18n 用例与 `tests/api/test_i18n_locale_cache_ratchet.py` 无回归

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 只替换双花括号，漏单花括号变体 | 中 | 正则 `/\{\{?\s*name\s*\}?/g` 兼容两种写法 |
| 长名在 AGModal 标题溢出 | 低 | 标题样式 `overflow-wrap/word-break` 落在 AGModal 自身（Teleport 根节点不继承调用方 scopeId，`:deep` 不生效） |
| 误改历史扁平键或其它弹窗 | 低 | 只新增降级键、只改删除弹窗 |
| 老客户端缓存住旧语言包 | 中 | 四页 cacheVer 联动 bump + 指纹更新 |
| 老客户端缓存住旧入口 JS/`all-views.css`（修复到不了端上） | 中 | 受影响入口/CSS `?v=` 联动 bump，`test_frontend_{cache_version,bundle_source}_ratchet.py` 守卫拦截 |