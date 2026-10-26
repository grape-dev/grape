# Vue 重构 · 跨会话续接状态（SESSION-STATE）

> 新会话的第一件事：让 Neko 读这个文件。用 @引用 或直接说"读 docs/weekly-08/migration/SESSION-STATE.md 续接 Vue 重构"。

## 项目背景
- 任务：grape 前端 Vue 重构（方案：docs/weekly-08/vue-refactor-design.md，盘点：同目录 vue-refactor-inventory.md）
- 分支：weekly-clean-mys（主题工作专用分支，动手前必须 `git branch --show-current` 确认）
- 所有 commit 只在本地，push/MR 需主人明示
- 每批协议硬步骤：功能验证 + **双主题截图** + **删除legacy源文件** + 更新迁移清单 + commit（曾漏过后两步被主人抓过，别再犯）

## 已完成（见 MIGRATION-LIST.md 有 commit hash）
- 阶段0/1：目录收敛 src/api/frontend/{app,legacy}、Vite+Vue3、@apple-ui组件库、/static-vue 挂载、bridge tokens
- 批1：beta-apply（/static-vue/beta-apply）、help（改生成器CSS）
- 批2：my_feedback×2、im_channel、organizations（壳内chunk方案）
- 批3：pipeline、agents（五视图共用AgentView）
- 批4：scene-market（市场+详情+四弹窗，renderSceneMarket/renderSceneDetail契约；坑：fragment根+根级Teleport会unmount崩，须单一根；renderSceneDetail入口须先设_currentView）

## 核心架构（每批照此办理）
- 壳内chunk：Vite 多入口 → viewjs/[name].js 拷到 legacy/js/vue/，共享js全量拷到 legacy/js/assets/
- 契约不变：老壳 index.html 换 `<script type="module" src="/static/js/vue/xxx.js?v=N">`，全局 renderXxx() 函数名/签名与老代码一致
- 桥接：src/styles/bridge.css 把新token(--c-text等)映射到老壳变量(--t/--a/--c)，自动跟随明暗；**暗色柔和层已加**（prefers-color-scheme 时降饱和）
- entry模板：注入 bridge.css + ag-ui.css + viewcss/xxx.css（带?v=）再 import Vue 视图
- 视图内用 `const G = globalThis` 访问老壳 I18N/API/app/_vcsUrl/fmtLocalTime（legacy api.js/app.js 尾部已加 window.API=API/window.app=app 桥接）

## 坑（踩过的，别再踩）
1. eslint --fix 会把模板内联多语句handler拆行导致Vue编译失败 → 一律用 methods
2. vite插件拷贝：共享chunk（_plugin-vue_export-helper等）必须全量拷，只拷AG*会加载失败
3. viewcss 正则要匹配实际产出名（小写连字符，如 agent-view-xxx.css）
4. 老壳暗色从不重定义彩色变量 → 视图别硬编码亮色hex，用bridge token或暗色友好值
5. var()+hex字符串拼接是非法CSS会被整条丢弃（agent头卡事故）
6. about:blank 上动态import绝对URL会失败，harness要用http页面
7. 测试脚本设 input.value 后必须 dispatch input 事件 v-model 才更新
8. 改 legacy JS/CSS 必须 bump index.html ?v=

## 验证环境
- API：background 跑 `python3 -m src.api.run`（端口3000；kill旧进程后ss -ltnp确认端口空闲再启）
- 无头Chrome：9222 端口常驻（/home/mys/.cache/ms-playwright/chromium-1243），CDP用 node + ws 包（app/node_modules里有）
- 登录基线：测试账号 vue-mig-test / VueMig-Test-2026（POST /api/auth/login 拿token种 localStorage）
- 无repo视图（im-channel/organizations等）用mock全局harness验证（help.html页面上种mock再import chunk）
- 中文字体已装（~/.local/share/fonts/NotoSansSC-Regular.ttf + fontconfig回退）
- 截图存 docs/weekly-08/screenshots/（明暗各一张是硬要求）

## 下一批
- skills.js(1058)（注意：技能详情返回会调 window.renderSceneDetail(param)——批4已在新chunk里实现该全局）
- 壳耦合组（后期统一批）：settings.js、repos.js+templates.js（templates是repos大厅的lobby子标签，已重分类）
- 大件队列：chat(1422) account(1552) events(1569) issues(1981) autopilot(2026) runtimes(2270) dashboard(2513) observability(3035) → admin独立站 → app.js外壳+index.html收口
- 死代码待主人裁决删除：agents.js的Configure面板函数（约250行无UI入口）
- help页是否彻底Vue化：阶段3裁决
- **视觉/体验债**：迁移中主人不满意的视觉效果统一记在 docs/weekly-08/ui-debt.md（5条：issues太黑/scene+im-channel太白/pipeline+runtimes+repos霓虹小配色/skills+knowledge+chat配色/dashboard卡片边界），迁移收口后批量处理，单批迁移不做视觉升级

## 批4新增坑（别再踩）
- Vue组件根模板是fragment（多节点）且含根级Teleport(AGModal) → unmount时nextSibling撞null直接崩，必须包单一根元素
- 入口直挂详情（renderSceneDetail(id)→initialDetailId）时必须先设 window._currentView='scene-detail' 再发起加载，否则stale守卫丢弃结果
- CDP调试：Chrome要求message id必须是整数，字符串id会被静默拒收（命令发出但无响应，脚本空等到超时）
- 测试账号vue-mig-test是0 bindings，scene-market在app.js需求17的_repoDependentViews里会被拦到引导页；验证时种假binding+sessionStorage currentRepo绕过（纯前端测试手段）
- 老代码 bgColor+'22' / var(--r)22 十六进制拼alpha是非法CSS整条丢弃，一律改 color-mix(in srgb, X N%, transparent)
- **双主题截图的正确姿势（10-24翻车修正）**：壳的暗色=data-theme-mode="auto"+浏览器prefers-color-scheme媒体查询，只设data-theme-mode="dark"attr是无效的（CSS里没有该组合的变量定义）→截出两张一样的亮图。裸CDP Emulation.setEmulatedMedia在这个headless chrome上也不生效，必须用playwright的page.emulate_media(color_scheme='dark')+localStorage grapeThemeMode='auto'，截完用PIL像素均值核对（亮~241/暗~36）。截图超时重试2-3次即可（swiftshader软渲染慢）。工具链：pip3 install --user --break-system-packages playwright -i https://pypi.tuna.tsinghua.edu.cn/simple（默认源会超时）

## 命令速查
- 构建+lint：cd src/api/frontend/app && npm run lint && npx vite build
- 迁移清单：docs/weekly-08/migration/MIGRATION-LIST.md（每批更新：模块/commit/验收/截图路径）
