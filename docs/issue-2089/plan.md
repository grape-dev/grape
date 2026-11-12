# Plan: Issue #2089 — 帮助抽屉全屏 + 窄容器自适应

## 切片拆解（TDD 红/绿分提交）

### 切片 1：抽屉头部按钮 + 全屏切换 + 新标签页（前端三件套 + i18n）
- seam：DOM 契约（index.html 元素 id/class）+ app.js 公开导出（`toggleHelpFullscreen`/`openHelpNewTab`）+ CSS 类契约（`.help-drawer.fullscreen`）+ i18n key。
- 红：`tests/js/test_issue_2089_help_drawer.js`（静态契约断言，先提交，应红）。
- 绿：index.html（`help-drawer-hdr-actions` 按钮组）→ app.css（`.fullscreen` 态 + 按钮组样式）→ app.js（toggle/openNewTab，iframe 不重建）→ i18n.js（zh/en 三 key）。
- 提交：`test(#2089)` → `feat(#2089)`。

### 切片 2：help_page_gen.py `_CSS` 宽度自适应 + 横向滚动
- seam：`gen._CSS` 字符串契约 + 重新生成 help.html 落盘产物。
- 红：`tests/test_help_page_generation.py` 新增 TestIssue2089 契约类（min(940px,100%)、.content overflow-x:auto、900px 断点回归）。
- 绿：`_CSS` 改 `.content` 两处 + `grape git` 提交；重跑生成器更新 `src/api/static/help.html`（生成产物一并提交）。
- 提交：`test(#2089)` → `feat(#2089)`。

## 顺序
1. 切片 1 红 → 绿 → 本地 node 测试过。
2. 切片 2 红 → 绿 → pytest 过 + 生成器重跑。
3. 一次性运行验证：起静态服务 → curl help.html 校验 CSS/断点 → kill。
4. 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base=local-liufei）→ Issue 评论 → A2A QA → tasks complete。

## 风险
- ESC 在全屏态直接关抽屉（而非先退全屏再关）——与浏览器全屏习惯一致，Spec 风险表「关闭前先退出全屏态」在实现上等价为 closeHelp 内不清 fullscreen 类、下次打开保持状态，交互无冲突。
- help.html 是生成产物：_CSS 改动后必须重跑生成器，否则测试读旧产物。
