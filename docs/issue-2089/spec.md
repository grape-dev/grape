# Spec: Issue #2089 — 帮助抽屉全屏/还原、新标签页打开与窄容器自适应

> 来源：Issue Agent 已发布 Spec（评论 id 200162340，评级 ready）。本文件为 Coding 侧落地方档，与已发布 Spec 一致。

## 目标
1. 抽屉头部新增「全屏/还原」切换按钮（全屏铺满可视区域，ESC/遮罩关闭无回归）。
2. 抽屉头部新增「新标签页打开」入口（`/static/help.html`，独立全屏页面）。
3. 520px 窄抽屉内 help.html 自适应：目录可展开（burger）、正文无截断、宽表格/长代码横向滚动。
4. 全屏↔还原切换 iframe 不重建，阅读位置（hash 路由 + scrollTop）不丢。

## In Scope
- `src/api/static/index.html`：`#helpDrawer` 头部按钮组。
- `src/api/static/css/app.css`：`.help-drawer.fullscreen` 全屏态 + `.help-drawer-hdr-actions` 按钮组样式。
- `src/api/static/js/app.js`：`openHelp/toggleHelpFullscreen/closeHelp`；ESC 处理器。
- `src/api/static/js/i18n.js`：`app.help.fullscreen/exit_fullscreen/open_new_tab` 中英文案。
- `src/help_page_gen.py`：`_CSS` 宽度自适应与横向滚动。

## Out of Scope
- 抽屉宽度拖拽与记忆（后续迭代）。
- help.html 内容/文案变更、生成逻辑重构。

## 技术方案
### 前端抽屉（index.html / app.css / app.js）
1. `index.html` 头部改为 `<h2> + <div class="help-drawer-hdr-actions">`，含三个 `.help-drawer-close` 同风格按钮：
   - `#helpNewTabBtn`（↗）：`window.open('/static/help.html')`。
   - `#helpFullscreenBtn`（⛶ / ⇲）：toggle CSS 类 `fullscreen` 于 `.help-drawer`，不重建 iframe。
   - 关闭按钮保持 `app.closeHelp()`。
2. `app.css`：`.help-drawer.fullscreen{width:100vw;max-width:100vw;height:100vh;transform:none;transition:width .2s,transform .2s}`；`.help-drawer-hdr-actions{display:flex;gap:4px}`；全屏态隐藏外层 body 滚动（`.help-drawer-body` 全屏下仍由 iframe 内部滚动，避免嵌套）。
3. `app.js`：
   - `openHelp()` 保持现状（iframe 只创建一次）。
   - 新增 `toggleHelpFullscreen()`：toggle `fullscreen` 类 + 切换按钮 title/aria-label + 图标（⛶⇲）。
   - 新增 `openHelpNewTab()`：`window.open('/static/help.html', '_blank', 'noopener')`。
   - `closeHelp()`：关闭时**不**移除 `fullscreen` 类（重开保持上次状态）——按验收「关闭后再打开行为一致」。
   - ESC 处理器不变（`document keydown Escape → closeHelp()`），全屏态同样关闭抽屉（体验同浏览器全屏退出习惯）。回归点：全屏态点遮罩不可关闭？—— Spec 风险表：「关闭前先退出全屏态」→ 采用：全屏态下遮罩仍在底层 z-index 9990 < 9991，但被抽屉铺满不可点。故全屏下 ESC 直接关闭抽屉（一步到位）。
4. i18n：`zh: fullscreen:'全屏', exit_fullscreen:'退出全屏', open_new_tab:'在新标签页打开'`；`en` 同步。

### help_page_gen.py `_CSS`
1. `.content{max-width:940px}` → `max-width:min(940px,100%)`：宽容器（全屏 ≥1200px）铺满。
2. 窄容器表格滚动：`.content` 加 `overflow-x:auto`（容器级横向滚动，宽表格在窄容器出滚动条不裁切）。
3. 900px 断点保留移动版式（`.side` fixed + burger 展开），无改动；`table`/`pre` 已有 `width:100%`/`overflow-x:auto`，容器级滚动兜底。
4. 验证：运行生成器产出 help.html，一次性静态服务 curl 校验关键 CSS 字段。

## 测试计划（TDD）
- **红 1（JS 契约）**：`tests/js/test_issue_2089_help_drawer.js` — 断言 index.html 含 `helpFullscreenBtn`/`helpNewTabBtn`、app.js 暴露 `toggleHelpFullscreen`/`openHelpNewTab`、app.css 含 `.help-drawer.fullscreen`、i18n 含新 key。先写测试（红）→ 改实现（绿）。
- **红 2（CSS 契约）**：`tests/test_help_page_generation.py` 增用例 — `_CSS` 含 `max-width:min(940px,100%)`、`.content` 含 `overflow-x:auto`、900px 断点 `.side.open` 仍存在（回归）。
- 运行：`node tests/js/test_issue_2089_help_drawer.js` + `python3 -m pytest tests/test_help_page_generation.py tests/test_help_directory.py`。
- 运行验证一次性模式：`python3 src/help_page_gen.py`（或对应生成入口）→ `python3 -m http.server` 后台 → curl 校验 → kill。

## 验收对照
- [ ] 抽屉头部全屏/还原按钮存在，图标随状态切换。
- [ ] 全屏态内容区宽 ≥1200px@1440px（CSS：width:100vw 铺满 → 满足）。
- [ ] iframe 不重建 → 阅读位置保留（toggle 类实现）。
- [ ] 520px 内目录 burger 可展开、宽表格横向滚动（`overflow-x:auto` 容器）。
- [ ] 新标签页入口存在，页面默认全屏（help.html 本身即全页）。
- [ ] ESC/遮罩关闭无回归；移动端 ≤768px、900px 断点无回归。
- [ ] 现有测试通过 + 新增覆盖。
