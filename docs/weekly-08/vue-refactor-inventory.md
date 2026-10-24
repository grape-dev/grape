# Grape 前端 Vue 重构 · 阶段 0 盘点报告

> 生成：2026-10-24 · 分支 weekly-clean-mys · 只读扫描，未修改任何文件、未运行构建。
> 扫描范围：`src/api/static/`（HTML 9 / js 27 / css 5 / locales 2 / img 7 / vendor 若干）+ `src/api/main.py` 静态挂载点。

## 1. 入口清单（9 个 HTML）

| 文件 | 行数 | 形态 | 处置 |
|---|---|---|---|
| index.html | 353 | SPA 壳：hash 路由 + 34 个 `<script>` 标签全量加载 | 最后迁（阶段2收尾批） |
| admin.html | 387 | 独立站：i18n.js + admin.js(926行) | 独立批次 |
| register.html | 506 | auth.js + auth.css | 独立批次 |
| forgot.html | 274 | auth.js | 独立批次 |
| beta-apply.html | 508 | 全内联（1 style + 1 script） | 独立批次（最简单整页） |
| help.html | 327 | 全内联 | 独立批次 |
| oauth-popup-callback.html | 205 | OAuth 回调 | **永不迁移** |
| issue-95-flow.html / issue-95-chat-flow.html | 209/181 | 历史原型流程页 | 建议直接归档删除（Gate 0 确认） |

## 2. 模块清单（js/pages/ 22 个，共约 2.3 万行）

路由：app.js 内 hash 分发，30 个 view 分支（含 skill/skill-sub/scene-detail 等子视图）。

| 模块 | 行数 | onclick | innerHTML | 备注 |
|---|---|---|---|---|
| my_feedback_detail.js | 143 | 3 | 6 | 叶子 |
| my_feedback.js | 146 | 6 | 5 | 叶子 |
| im_channel.js | 203 | 4 | 3 | 叶子 |
| settings.js | 309 | 7 | 8 | 叶子 |
| organizations.js | 349 | 18 | 12 | 小 |
| feedback.js | 368 | 6 | 1 | 小 |
| pipeline.js | 422 | 5 | 2 | 小 |
| agents.js | 526 | 5 | 6 | 小 |
| templates.js | 846 | 15 | 10 | 中 |
| repos.js | 821 | 30 | 11 | 中 |
| skills.js | 1058 | 42 | 24 | 中 |
| scene-market.js | 1069 | 38 | 30 | 中 |
| knowledge.js | 1085 | 0 | 1 | **已有 Vue app（_knowledgeVueApp）**，迁移起点参考 |
| chat.js | 1422 | 20 | 41 | 大 |
| account.js | 1552 | 56 | 21 | 大 |
| events.js | 1569 | 43 | 22 | 大 |
| issues.js | 1981 | 61 | 32 | 大 |
| autopilot.js | 2026 | 65 | 38 | 大 |
| runtimes.js | 2270 | 74 | 49 | 大 |
| dashboard.js | 2513 | 71 | 48 | 大，场景编辑核心 |
| observability.js | 3035 | 68 | 89 | 大，**echarts 唯一用户** |

公共层：app.js 2844（外壳+路由+导航+登录态）、api.js 543、auth.js 267、i18n.js 784、auto-recovery.js 146、admin.js 926（独立）。

## 3. 构建形态

- **无任何打包配置**。index.html 用 34 个带 `?v=` 的 script 标签直接加载；FastAPI `main.py:1008` 一行 `app.mount("/static", StaticFiles(STATIC_DIR))` 伺服。
- vendor 全局件：jquery、marked、qrcode、select2(+css)、echarts、**vue.global.prod.min.js（Vue3 全局构建已在用！）**。

## 4. 样式形态与全局影响面

| 文件 | 行数 | 影响面 |
|---|---|---|
| tokens.css | 24 | 变量基础，仅亮色 |
| app.css | 1140 | 全站，含 accountModal/bindPanel 等弹窗样式 |
| apple-theme.css | 1582 | 全站主题层（苹果风已有大半） |
| auth.css | 782 | 仅 register/forgot |
| select2.min.css | - | vendor |

隐性依赖：页面大量依赖 id 选择器（app.css 引用 12 个 id）；innerHTML 拼接 HTML 字符串是主要渲染方式（合计 500+ 处）——**这是迁移最大的工作量来源**。

## 5. 依赖图（简化）

```
index.html ── app.js(外壳/路由) ──┬─ 22×pages/*.js（互相基本独立，靠 window 全局函数通信）
                                  ├─ api.js（fetch 封装，全部页面依赖）
                                  ├─ i18n.js（全部页面依赖，t()失配返回key陷阱）
                                  ├─ auto-recovery.js（仅壳）
                                  └─ vendor: jquery/marked/qrcode/echarts/select2/vue.global
register/forgot.html ── auth.js + i18n.js + api.js + auth.css
admin.html ── admin.js + i18n.js（不依赖 api.js）
```

强耦合点：
- app.js ↔ dashboard.js（角色/仓库缓存共享，`_cachedReposData` 等）
- app.js ↔ account.js（延迟调用）
- observability.js → echarts（唯一）
- knowledge.js → 已是 Vue app，但挂在全局 `_knowledgeVueApp`，由 app.js 路由感知（`view === 'knowledge'` 特判）

## 6. 风险资产（行为等价清单必查项）

1. FOUC 防闪屏：head 内联 JS 设 `data-logged-in`（已确认存在）。
2. localStorage 键：`grapeTheme` / `grapeThemeMode` / `grapeLang` / `token` / `user` / `currentRepo` / `current_org_id` / `sidebarTab` / `app_cache_ver`。
3. i18n：t(key) 失配返回 key 本身；zh/en 双 locale 文件。
4. 页面间通信靠 window 全局函数（issues.js 暴露 `_jumpToPipeline` 等跨页跳转函数）——Vue 化后需事件总线等价承载。
5. runtimes.js 有 "stale cache 强制 reload" 逻辑（app.js 注释可见）。
6. CSS 压缩/写文件历史事故对策已写入方案 §工作协议。

## 7. 工作量与批次顺序建议

叶子→核心（每批一模块，大模块可再拆为 2~3 个子批同 commit 前先报告）：

- **批1（试点）**：beta-apply.html + help.html（全内联整页，练手建流程）
- **批2~4（叶子群）**：my_feedback_detail → my_feedback → im_channel → settings → organizations → feedback
- **批5~7（中）**：pipeline → agents → templates → repos → skills → scene-market
- **批8（参考）**：knowledge.js（已 Vue，收编入新工程）
- **批9~13（大）**：chat → events → account → issues → autopilot → runtimes → dashboard → observability
- **批14**：register + forgot（auth.css 一并收编）
- **批15**：admin 独立站
- **批16（最后）**：app.js 外壳 + index.html（路由全部 Vue Router 化，legacy 清零）

## 8. Gate 0 待主人拍板的三项

1. **Vite 产物伺服**：A（推荐，build 输出 `static/dist/`，FastAPI 挂载不动，仅入口替换，迁移完删临时挂载）/ B（新 `/app` 前缀双入口，需核对 oauth 回调）。
2. **无头登录基线**：A（推荐，测试账号真实登录流 + Playwright session 复用）/ B（后端加本地 mock 登录开关，需单独批）。
3. **issue-95 两个流程页**：确认是历史原型，直接删除归档？

另请确认：批次顺序、并存期视觉差异容忍度、唯一风格=苹果毛玻璃明暗双生。
