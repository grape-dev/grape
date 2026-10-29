# Vue 重构 · 全量交接状态（HANDOFF-STATE）

> 本文件是 2026-10-28 会话末的**全量交接快照**（批1-16 全部完成，js/pages 清零）。新会话续接顺序：
> ① 读 `docs/weekly-08/migration/SESSION-STATE.md`（每批坑/协议速查）→
> ② 读本文件（全局口径 + 未完成事项）→
> ③ 开工前 `git branch --show-current` 确认在 weekly-clean-mys。
> 日常每批细节看 SESSION-STATE.md / MIGRATION-LIST.md；本文件管全局。

---

## 1. 分支与提交状态（2026-10-28 会话末，批16 完成）

- **分支**：`weekly-clean-mys`（Vue 重构专用分支，动手前必须 `git branch --show-current` 确认——仓库可能被别的会话切到 fix/hotfix-*）
- **工作树**：干净（仅 1 个未跟踪 `.hermes-handoff-pr-subscription.md`，与本工程无关）
- **Git 红线**：所有 commit 只在本地；**push / 建分支 / MR 需主人明示**。无 MR 开设中。
- 本会话产生的 commit 链（新→旧，全部在本地）：

| commit | 内容 |
|---|---|
| `fee3b30bb` | **feat: 批16 feedback → Vue（js/pages 目录清零 🏁）** |
| `0892e6150` | **feat: 批15 knowledge → Vue** |
| `635aac467` | **feat: 批14 壳耦合组 settings+repos+templates → Vue** |
| `9905c518c` | docs: add full handoff state for vue refactor (batches 1-13) |
| `cad68fac5` | docs: 批13 observability 完成记录 |
| `161d3b694` | **feat: 批13 observability → Vue（3034行，最后大件）** |
| `c2182b2d3` | docs: 批12 dashboard 完成记录 |
| `92afb2226` | **feat: 批12 dashboard → Vue（2513行）** |
| `c609fceaa` | docs: 批11 runtimes 完成记录 |
| `fc1dc3bdd` | **feat: 批11 runtimes → Vue（2269行）** |
| `2ba50004b` | docs: 批10 autopilot 完成记录 |
| `2db55c623` | **feat: 批10 autopilot → Vue（2025行）** |
| `11ff4443a` | docs: ui-debt #6（需求详情字色撞背景）记账 |
| `e08ddc3f1` | docs: 批9 issues 完成记录 |
| `0206b013a` | **feat: 批9 issues → Vue（1981行）** |
| `71336f508` | docs: 上一会话的 handoff 刷新 |

（更早的批1-8 commit 见 MIGRATION-LIST.md 表格，均有 hash。）

## 2. 迁移进度总览

**批1-16 全部完成。`js/pages/` 目录已清零——22个模块约2.3万行全部迁移完毕。**

Vue 侧现状：`app/src/views/` 22 个 .vue 组件（Dashboard 3891 / Observability 3874 / Issues 3532 / Autopilot 3346 / Runtimes 3323 / ReposView+TemplatesView+SettingsView 合一批 / KnowledgeView / FeedbackDialog（全局弹窗组件）/ Event 2237 / Account 2173 / SceneMarket 1988 / 其余更小）+ `entries/` 19 个入口 chunk。

每批窗口契约（entry 持有 window 全局函数，app.js nav 路由段调用，函数名/签名与老代码一致）：
- 批9 issues：`renderTasks(param)`；导出 `_stopScenePoll/_grapeConfirm/_backToTaskDetail/_jumpToNodeEvent/_jumpToPipeline/_tasksById`
- 批10 autopilot：`renderAutopilot()` / `renderAutopilotDetail(id)`；复用批8 `openTaskLog` + app.js `_cf*`
- 批11 runtimes：`renderRuntimes()`；app.js 路由段已改调它（app.js **v188**）；dashboard 桥接已随批12 撤除
- 批12 dashboard：`renderDashboard(data)` / `renderSceneDetailLocal(id)`；`window._dashboardEditing` 与 app.js 可见性守卫互通
- 批13 observability：`renderObservability(section)`；`window.stopObsAuto`（app.js typeof 守卫）；echarts 走 vendored 全局 `G.echarts`、mermaid 保持 CDN 动态加载
- 批14 壳耦合组：`renderRepoSelection(repos)` / `renderRepos()` / `renderSettings(tab)` / `switchSidebarTab(tab)` / `selectRepo` / `platformIcon`（裸标识符，app.js onboarding 用）/ `openCreateRepoModal` 由 entries/repos-view.js 持有；templates 是 ReposView 内子 tab 组件；死代码不迁（settings 的 theme/lang/apikeys 函数已由批7 AccountView 承载、_loadOrgSection 无渲染者）
- 批15 knowledge：`renderKnowledge()` + `_knowledgeVueApp` 裸标识符句柄（app.js 1296/1375/1562 三处守卫）由 entries/knowledge-view.js 持有；句柄只在真 mount 后设置（防拦截首次 nav）；散装 Vue app 收编（Options API → 组合式 SFC）
- 批16 feedback：`openFeedbackDialog(opts)` 全局弹窗（非页面视图）由 entries/feedback-dialog.js 持有，单例挂 body 隐藏宿主；app.js 顶栏裸调用 + EventView 带 event_seq

## 3. 改动口径（不变式，每批照此办理）

1. **壳内 chunk 架构**：Vite 多入口 → `viewjs/[name].js` 拷到 `legacy/js/vue/`，共享 js 全量拷 `legacy/js/assets/`；index.html 换 `<script type="module" src="/static/js/vue/xxx.js?v=N">`；**同批次重建必须 bump ?v=**（浏览器缓存中间坏版本）。
2. **契约不变**：全局 `renderXxx()` 函数名/签名与老代码一致；entry 层持有 + fresh mount + props 传初始参数；组件内不覆盖全局。
3. **窗口全局桥接**：凡 app.js / 其他 pages 引用裸全局（`_grapeConfirm` / `_cfNameError` / `fmtLocalTime` / `_vcsUrl` / `platformIcon` / `openTaskLog` 等），entry 或组件尾部补 `window.x = x` 并 bump 对应壳文件 `?v=`。批11 曾为 dashboard 桥 `_sceneDisplayName` 等 3 全局，批12 已撤——目标模块迁移后撤桥。
4. **改 legacy JS/CSS 必 bump index.html `?v=`**；改 `vite.config.js` 后 `grep -c "entries/"` 对数（本批末应为 **16**）。
5. **每批硬步骤**：npm run lint（0 errors）+ vite build + 无头 Chrome mock harness 全流程断言 + **双主题截图**（playwright emulate_media + viewport 1440×900 + PIL 像素核对，亮~245/暗~30）+ **删除 legacy 源文件前 grep 其它 pages 引用** → git rm → 更新 MIGRATION-LIST.md + SESSION-STATE.md → commit（英文、只本地）。
6. **行为等价，不做视觉升级**：迁移引入的功能 bug 当场修；视觉/体验问题记 `docs/weekly-08/ui-debt.md`，收口后批量处理。
7. **app.js 路由段改动**：迁一个 view 改一段（runtimes 段已改，bump v188）；最终收口批统一清理。

## 4. 高频坑速查（批13 末状态，详细版在技能 references）

1. **模板内联多语句 handler 被 lint:fix 拆行 → Vue 编译崩**（批13 炸了 3 轮共 6 处）：**一律用 methods，一处都别写内联多语句**。
2. **radio/select 的 `value="s"` 是字面量**：必须 `:value="sc"`（批11 第 4 犯）。
3. **`ref(false)` 配模板 `.open` 访问静默不渲染**：布尔开关+模板 `.xxx` 访问用 `reactive({open:false})`。
4. **多模式视图（同组件承载 list/detail/create）**：每个模式入口函数里都要设 `loaded=true`，否则 fresh mount 后永远 loading（批12）。
5. **window 全局标志不能 setup 快照**：深链/`_pendingXxx` 必须在加载回调里实时读 window（批9）。
6. **harness**：CDP `Page.navigate`/reload 会清 mock → 先种 mock 再用 `app.nav()` 导航；reload 后 sessionStorage 可能被守卫踢到 repos，须重种 repo 再 nav；`localhost`/`127.0.0.1` origin 不一致会让 hash nav 静默失败；observability 这类混用 `API.get`+裸 `fetch` 的页面要**双通道劫持**；调试信息存 `window.__xxx` 再 evaluate 读（console.log 收不到）。
7. **echarts**：vendored 全局 `G.echarts.init(el)` + 函数 ref 收集容器 + nextTick；**勿 import**（重复打包 1MB）。mermaid 保持动态 script 注入。
8. **computed 内不能写 window 副作用**（no-side-effects）：拆 `watch(x, …, {immediate:true})`。
9. **harness 断言选择器精确到 `.ap-nd-name`** 这类标签元素，整卡 textContent 模糊匹配会命中错误卡片（假阴性）。
10. **`createApp().mount()` 返回值才是根组件实例**：`app._instance` 生产构建为 undefined，defineExpose 方法从 mount 返回值取（批14）。
11. **fragment 根 + 根级 Teleport 炸 unmount**（批4/批15 两犯）：必须包单一根 div。
12. **entry 的 window 句柄必须真 mount 后才设**（批15）：模块加载即设会拦截 app.js 首次 nav 守卫。
13. **harness mock 必须返回深拷贝**（批15）：Vue3 ref 同引用赋值是 no-op，真实后端每次返回新 JSON。
14. **reload 后 hash 自动恢复会让登录前 mount 抢跑**（批15）：loadList 401 → firstLoad 卡 true；harness 先 nav away 再 nav 回。
15. **CDP 原生 `.click()` 在 Teleport 弹层内不触发 Vue handler**（批15）：用 `dispatchEvent(new MouseEvent('click', {bubbles:true}))`。
16. **弹窗类组件截图 PIL 判定**（批16）：overlay 非整页，均值判定失真——用亮像素占比（亮>10% vs 暗~0%）。

## 5. 未完成事项（按优先序）

### 5.1 剩余工作（收口阶段，无页面迁移批次了）
1. **admin 独立站** → 独立页面迁移。
2. **`app.js` 外壳 + `index.html` 收口批**（最后一批）：清死代码、统一路由段、撤全部剩余桥接（window.x 临时桥）、vue chunk `?v=` 统一梳理（现为 v1~v6 混杂）、`_showAppleConfirm` 等 app.js 尾部全局函数归属梳理。
3. 收口后 → 视觉债批量处理（见 5.2）。
4. 死代码待主人裁决：agents.js Configure 面板（约250行无UI入口）；help 页是否彻底 Vue 化（阶段3裁决）。

### 5.2 视觉/体验债（`docs/weekly-08/ui-debt.md`，迁移收口后批量处理）
- #1 issues 太黑 / #2 scene+im 太白 / #3 霓虹小配色 / #4 skills+knowledge+chat 配色 / #5 dashboard 卡片边界 / #6 需求详情字体色撞背景 / #6a dashboard 毛玻璃白底暗色过亮
- 处理原则已定：统一 tokens.css `--c-*`/`--glass-*`，明暗双生，逐页验收。

### 5.3 验证环境（本机常驻）
- API：background `python3 -m src.api.run`（:3000；占用时 `ss -ltnp | grep :3000` kill 旧 PID）
- 无头 Chrome：:9222 常驻；CDP 用 node + ws（`NODE_PATH=./node_modules` 在 app/ 下跑）；**CDP message id 必须整数**
- 登录基线：`vue-mig-test / VueMig-Test-2026`（POST /api/auth/login 种 localStorage）
- playwright：`p.chromium.connect_over_cdp`（不是 `p.connect_over_cdp`）；暗色截图用 localStorage `grapeThemeMode=dark`（方案A后直接设 attr 即可）
- 截图存 `docs/weekly-08/screenshots/`（明暗各一张是硬要求）
- 测试账号 0 bindings：repoDependent 视图种假 binding + sessionStorage currentRepo 绕过

### 5.4 遗留技术债（非阻塞）
- `index.html` 的 vue chunk `?v=` 均为 v1/v2——收口批建议统一梳理
- batch 间 lint:fix 产生的格式重排 diff 混在功能 diff 里（review 时 `git diff | grep '^-.*function\|^[+].*function'` 区分）
- i18n：新文案中文；`t()` 失配返回 key 本体，禁 `||` 兜底（用「值===key 视为未命中」判定，批13 的 `tOr` 模式）

## 6. 主文档索引

| 文件 | 用途 |
|---|---|
| `docs/weekly-08/migration/SESSION-STATE.md` | 每批契约+坑（新会话第一件事读它） |
| `docs/weekly-08/migration/MIGRATION-LIST.md` | 迁移清单（每批：模块/commit/验收/截图路径） |
| `docs/weekly-08/ui-debt.md` | 视觉债 7 条（收口后批量处理） |
| `docs/weekly-08/vue-refactor-design.md` | 总方案 v2 |
| `~/.hermes/skills/software-development/grape-project/references/vue-refactor-plan.md` | 技能侧主参考（批1-13 坑全集） |
| 同目录 `headless-screenshot-pipeline.md` | CDP/截图管线 |

## 7. 主人偏好备忘（本会话确认）

- 交付汇报必须含：**截图在哪、更新了哪些文件**（批9 起执行）。
- commit 全英文；MR 不关联 issue、描述纯英文。
- 「提交」=只 commit 本地；push/分支/MR 必须主人明示。
- 改动用「人话」解释：改了什么/为什么/影响什么。
- 证据交付硬标准：截图 PIL 像素自检、mock 手段主动坦白。
