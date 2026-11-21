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
     var name = deleteSkillName.value
     return tc2('skills.confirm.delete_skill')
       .replace(/\{\{?\s*name\s*\}?\}/g, function () { return name })
   })
   ```
   - 正则兼容 `{{name}}`（嵌套词条）与 `{name}`（历史扁平键写法），`\s*` 容忍空格；
   - **替换值必须是函数**：`String.replace` 的字符串替换值会解释 `$&`/`$1`/`` $` ``/`$'` 等模式，
     技能展示名一旦含 `$`，标题会被插入模板片段、丢掉原名；函数返回字面量，任意名称原样渲染
     （评审 Optional 项，`T9` 用例覆盖）。

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

6. **静态入口/CSS 缓存版本联动抬档**（评审 Required）

   本次改动落在 `AGModal.vue`（AG 组件样式聚合 `ag-ui.css` → `all-views.css`）与 `SkillsView.vue`，
   受影响的不只是语言包，还有老壳的静态入口与 CSS。按仓库两道棘轮守卫的口径抬档：

   | 抬档对象 | 位置 | 版本 |
   |---------|------|------|
   | `all-views.css` | `shell/index.html:13` | 67 → 68（`ALL_VIEWS` 指纹同步） |
   | `skills-view.js` | `shell/index.html:277` | 60 → 61 |
   | `scene-market.js` / `organizations.js` / `plugins.js` | `shell/index.html:276,278,282` | 61→62 / 80→81 / 56→57（AGModal 使用方） |
   | `autopilot-view.js` / `dashboard-view.js` / `event-view.js` / `runtimes-view.js` / `sandbox-view.js` | `shell/index.html:268,271,280,286,287` | 66→67 / 88→89 / 63→64 / 89→90 / 13→14（AGModal 使用方） |
   | 入口运行时注入的 CSS | `views/entries/{autopilot,dashboard,event,runtimes,sandbox}-view.js` | 63→64 / 64→65 / 63→64 / 68→69 / 5→6（`ag-ui.css` 内容已变） |
   | `tests/api/test_frontend_{cache_version,bundle_source}_ratchet.py` | 冻结表 | 页面版本 + 源指纹（`FROZEN`/`SHIPPED` 同抬） |

   - **`admin.html` 不抬档**（核对结论）：`AGModal.vue` 的实际 import 只有 `SkillsView` / `SceneMarket` /
     `Organizations` / `PluginMarketView`（以及 dashboard 等 AG 组件使用方）；`AdminView` / `AdminChannels` /
     `AdminPlatforms` **没有** import `AGModal`（关闭平台确认层是自写的 `.ap-confirm-dialog` 结构），
     `admin.html` 也不加载 `all-views.css`。棘轮实测 `admin-view.js` 源指纹未变（`e59a18dcd51cee02`），
     对未变更产物抬版本属于无内容变更的越权抬档，故不改。
   - 顺带对齐既有漂移：`tests/api/test_frontend_cache_version_ratchet.py` 的 `faro.js` 冻结值 7 → 8
     （基线 `arch-weekly-11-ljt` 上页面已是 `?v=8`，守卫在基线即已红，与本 Issue 改动无关）。

## 测试 seam

前端无组件单测框架，沿用仓库既有 `tests/js` **源码级断言**：
- seam = 删除弹窗模板片段 + `deleteTitle`/`deleteSkillName` 定义 + 语言包 JSON；
- 从 SFC 中**抽取真实正则字面量**再对 zh/en 模板做替换，验证「替换后无占位符残留」，
  避免测试自造一份与实现脱节的逻辑。

## 兼容与回归

- `askDelete` / `confirmDelete` / 删除接口 / 按钮 loading 文案一律不动；
- `tc2()` 本身不改（当前仅该弹窗使用），`confirm.delete_skill` 词条不动；
- 转换（复制）弹窗的 `{name}` 单花括号逻辑不动。