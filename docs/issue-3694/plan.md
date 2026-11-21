# Issue #3694 方案设计

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-21

## 方案要点

1. **被删名 computed**（`SkillsView.vue`）
   ```js
   const deleteSkillName = computed(function () {
     const n = (titleOf(deleteCtx.skill) || '').trim()
     return n || tt2('confirm.delete_skill_fallback')
   })
   ```
   - 数据源 = `deleteCtx.skill`（`askDelete` 每次打开都覆盖赋值），天然随被删对象变化；
   - `titleOf()` 已实现 `display_name || name`，不新增取名口径；
   - 空名降级走新增词条 `skills.confirm.delete_skill_fallback`，并在 `KEY2_DEFAULTS` 补中文兜底
     （与文件既有「i18n 缺包时中文兜底」约定一致）。

2. **标题 computed**：取原模板后只替换 `name` 占位符，不改 `tc2()` 全局语义
   ```js
   const deleteTitle = computed(function () {
     return tc2('skills.confirm.delete_skill')
       .replace(/\{\{?\s*name\s*\}?\}/g, deleteSkillName.value)
   })
   ```
   - 正则兼容 `{{name}}`（嵌套词条）与 `{name}`（历史扁平键写法），`\s*` 容忍空格；
   - 替换值来自 computed，无二次模板注入风险（`AGModal` title 以文本节点渲染，自动转义）。

3. **模板去重**：`:title="deleteTitle"`；正文删除重复 `<h3>`，仅保留 `.sk__conf-ic`（🗑 图标位）
   + `delete_skill_tip`。`sk__conf h3` 样式保留（转换弹窗仍用）。

4. **长名样式**：放在 `AGModal.vue` 自身的 `.a-modal__title`
   ```css
   overflow-wrap: anywhere;
   word-break: break-word;
   ```
   **实施偏差（已实测）**：Spec 原计划在 `SkillsView.vue` 写 `:deep(.a-modal__title)`。该组件根节点是
   `Teleport to="body"`，Vue 只把父组件 scopeId 传给「子组件根元素」，Teleport 的根节点不参与这次传递
   （jsdom + 同版本 runtime 复现：弹窗标题节点仅有 AGModal 的 `data-v-*`，无 SkillsView 的），
   编译结果 `[data-v-skills] .a-modal__title` 永远命中不了（构建产物 `[data-v-e59b57d8] .a-modal__title` 可验证）。
   因此把换行规则放回 AGModal 自身样式：只影响标题排版，对其它弹窗是防御性增强，无行为变更。

5. **i18n**：`zh/en skills.json` 的 `confirm` 对象新增 `delete_skill_fallback`
   （zh `该技能` / en `this skill`）；保留 `confirm.delete_skill` 的成对 `{{name}}`
   （#3355 插值契约口径不变）；按仓库 ratchet 纪律 bump 四页 cacheVer 并更新
   `tests/api/test_i18n_locale_cache_ratchet.py` 指纹。

## 测试 seam

前端无组件单测框架，沿用仓库既有 `tests/js` **源码级断言**：
- seam = 删除弹窗模板片段 + `deleteTitle`/`deleteSkillName` 定义 + 语言包 JSON；
- 从 SFC 中**抽取真实正则字面量**再对 zh/en 模板做替换，验证「替换后无占位符残留」，
  避免测试自造一份与实现脱节的逻辑。

## 兼容与回归

- `askDelete` / `confirmDelete` / 删除接口 / 按钮 loading 文案一律不动；
- `tc2()` 本身不改（当前仅该弹窗使用），`confirm.delete_skill` 词条不动；
- 转换（复制）弹窗的 `{name}` 单花括号逻辑不动。