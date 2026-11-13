# Grape 前端 Vue 重构执行方案 v2（修订版）

> 定位：给执行助手的作战手册 + 给项目负责人的审批清单。
> 与 v1 的差异：批次单位改为「模块」、补齐 Vite 产物伺服方案、暗色双主题从第一天进入 tokens、
> 明确 FastAPI 静态伺服现实、缓存版本号策略、无头登录基线、commitlint 与 Git 红线的兼容。
> 前置事实（2026-10-24 实测）：当前前端位于 `src/api/static/`，无构建工具，
> 形态 = index.html 壳 + `js/pages/` 20 个页面模块（约 2.4 万行）+ 5 个独立 HTML + vendor 依赖（echarts / select2）。

---

## 0. 目标与硬约束

### 0.1 目标

1. **统一目录**：所有前端代码收敛到唯一根目录 `src/api/frontend/`（v1 写的 `frontend/` 移到 `src/api/` 下，
   与 FastAPI 同域伺服，避免跨目录挂载）。迁移期老代码整体搬入 `src/api/frontend/legacy/`，第一天就收敛。
2. **渐进迁移**：`js/pages/` 模块 + 独立 HTML 逐个迁移为 Vue 3 页面，业务逻辑、接口调用、数据结构保持等价。
3. **风格统一**：苹果毛玻璃（Liquid Glass）+ 同一语言的暗色双生为唯一视觉规范，沉淀为 tokens + 组件库；
   现有 `apple-theme.css` / `auth.css` 里的旧写法随迁移自然淘汰。
4. **规范收口**：ESLint / Prettier / EditorConfig / commitlint 落地，机器约束贡献者风格差异。

### 0.2 硬约束（不商量）

1. 只改渲染层与文件组织，不修改业务逻辑、接口调用、数据结构、后端代码。
   （唯一例外见 §4.2：FastAPI 加一行静态挂载 + index.html 入口指向，属方案内已批准的改动。）
2. 每批一个 commit，可单点回滚。
3. 任何提交前必须通过验证（build / lint / 截图对比）；验证失败只能修产物，不许绕过。
4. 未迁移的文件不得删除；`legacy/` 里的文件只能由「迁移完成并验收通过」这一种方式移除。
5. 新需求页面从立项起直接写 Vue + 组件库，禁止再往 `legacy/` 加代码。
6. **Git 红线不变**：commit 只落本地；push / 建分支 / MR 必须主人明示。每批 Gate 通过 ≠ 自动 push。
7. 主题工作只在 `weekly-clean-mys` 分支进行（动手前 `git branch --show-current` 确认，别的会话可能切走分支）。
8. **永不迁移清单**（阶段 3 清零校验时按「合法保留」处理，不视为孤儿）：
   - `oauth-popup-callback.html`（OAuth 弹窗回调，动它 = 登录炸）
   - `vendor/`（echarts / select2 等三方件）
   - `docs/`、`examples/` 下的原型 HTML（不属于运行时前端，不卷进 legacy）

---

## 阶段总览

| 阶段 | 名称 | 产出 | Gate（主人审批） |
|---|---|---|---|
| 0 | 代码盘点 | 《盘点报告》+ 依赖图 + 批次顺序 + 三个待拍板项 | 风格/批次/伺服方案/登录方案 |
| 1 | 地基与规范 | frontend/ 收敛、Vite+Vue3、规范文件、设计系统(明暗双主题) | 抽查组件库双主题截图 |
| 2 | 批次迁移 | legacy/ 逐模块清空、每批 commit | 每批抽查 1~2 页 + diff review |
| 3 | 收尾收敛 | legacy/ 清零、孤儿清理、README、最终验收 | 最终交付确认 |

---

## 阶段 0：代码盘点（只读只统计，不写任何代码，不跑构建）

### 1.1 盘点范围

`src/api/static/` 全量扫描（HTML / js / css / locales / img / vendor），
另核对 `src/api/routes/` 中静态文件伺服与 CSP 相关代码路径（只读，为 §4.2 做输入）。

### 1.2 盘点清单

| # | 盘点项 | 说明 |
|---|---|---|
| 1 | 入口清单 | 9 个 HTML（index 壳 + register/forgot/admin/beta-apply/help/oauth-callback + 2 个 issue-95 临时流程页→确认是否可直接归档） |
| 2 | 模块清单 | `js/pages/` 20 个模块逐一：行数、路由挂载方式、依赖的公共 JS |
| 3 | 构建形态 | 确认无打包配置，FastAPI 如何伺服 static/ |
| 4 | 样式形态 | app.css / apple-theme.css / auth.css / tokens.css 各自影响面；**全局选择器影响面重点标注**（隐性 CSS 依赖） |
| 5 | 公共代码 | app.js / api.js / auth.js / i18n.js / auto-recovery.js 被谁引用 |
| 6 | 依赖图 | 模块→公共代码引用图（mermaid），标出强耦合核心 |
| 7 | 风险资产 | FOUC 内联 JS、`?v=` 版本号、localStorage 主题键、i18n 陷阱、echarts/select2 用点 |
| 8 | 工作量估算 | 按模块行数折算批次大小 |

### 1.3 盘点产出

1. `docs/weekly-XX/vue-refactor-inventory.md`：上述清单 + 每模块功能要点（交互点列表，阶段 2 验收用）。
2. 依赖图（mermaid）。
3. 迁移批次顺序建议：叶子模块优先（如 my_feedback_detail、feedback 等独立页）→
   中等耦合 → app.js 外壳 + admin 独立站最后。
4. **三个待拍板项**（附 A/B 选项）：
   - Vite 产物伺服方案（见 §4.2）
   - 无头登录基线方案（见 §5.2）
   - 暗色 tokens 结构确认（见 §4.4）

### Gate 0

主人确认：唯一风格（苹果毛玻璃明暗双生）、批次顺序、伺服方案 A/B、登录方案、并存期视觉差异容忍度。

---

## 阶段 1：地基与规范

### 4.1 目录收敛

```
src/api/frontend/
├── package.json
├── vite.config.ts          # build.outDir = ../static/dist（回填方案B）或独立伺服（方案A）
├── index.html              # Vue 入口（新）
├── eslint.config.js / .prettierrc / .editorconfig / commitlint.config.js
├── src/
│   ├── main.js / App.vue / router/
│   ├── components/ui/      # @apple-ui 组件库（Button/Input/Modal/Table/Nav/Card/Form/Toast…8~12个）
│   ├── pages/              # 迁移完成的新 Vue 页面
│   ├── styles/tokens.css   # 明 + 暗 双组 CSS variables（§4.4）
│   ├── composables/        # api/i18n/theme 等 Vue 化封装（等价包装现有 js，不改行为）
│   └── utils/
└── legacy/                 # src/api/static 原结构整体移入（html/js/css/locales/img）
```

- 老代码整体搬迁保持原目录结构，FastAPI 伺服路径同步改指向 `frontend/legacy/`（这一步是文件移动 + 挂载路径修改，单独一个 commit，改完立即全站冒烟 + 截图对比首页/登录/一个业务页）。
- 老代码不接入 Vite 构建，只做 EditorConfig + Prettier 文件级规范（可选），避免迁移前引入构建风险。

### 4.2 Vite 产物伺服方案（Gate 0 拍板，二选一）

- **方案 A（推荐）**：`vite build` 输出到 `src/api/static/dist/`，FastAPI 现有 StaticFiles 不动，
  仅 index.html 入口换成 dist 产物；legacy 继续由原路径伺服。后端改动 ≈ 1~2 行挂载，迁移完删。
- 方案 B：FastAPI 新增 `/app` 前缀挂载 frontend 产物，新旧两入口并存，nginx / oauth 回调地址需核对。

选择 A 的理由：URL 不变、oauth redirect 不受影响、回滚只是换回 index.html。

### 4.3 规范文件

- ESLint（vue/flat config）+ Prettier + EditorConfig；`legacy/` 全量豁免，收编一页删一页豁免，不留豁免残留。
- husky + lint-staged + commitlint：`type(scope): subject`，type 白名单含 `migrate`。
- commitlint 只约束格式；**push 与否由主人决定**（Git 红线），工具不自动 push。

### 4.4 设计系统（最核心，明暗双生从第一天开始）

1. tokens.css：基于现有 `apple-theme.css` 提炼而非从零发明——色板（莫兰迪场景色保留：vod `#B07A8B` 等）、
   间距阶梯、圆角（18px 卡片 / 999px 药丸）、字号、毛玻璃规范
   （亮：`rgba(255,255,255,.6)+blur(16px)`；暗：同语言的深色版），阴影 0 1px 4px 极轻、无描边。
2. **结构**：`:root[data-theme="light"]` 与 `:root[data-theme="dark"]` 两组变量，组件只引用变量。
   切换沿用现有 localStorage 主题键 + FOUC 防闪屏逻辑（等价迁移到 Vue 入口，属行为等价项）。
3. 组件库只做苹果毛玻璃一种风格，禁止双风格组件；README 写明禁手写魔法值。
4. **缓存策略**：Vue 侧由 Vite 文件指纹天然解决；legacy 侧改动仍执行「改 CSS 必 bump `?v=`」纪律直到清零。
5. 验收：组件示例页**明暗两版截图**各一份给主人，抽查 3~5 个组件还原度。

### Gate 1

主人抽查组件库双主题截图 + 规范文件，通过后进入迁移。

---

## 阶段 2：批次迁移

### 5.1 批次单位（与 v1 的关键差异）

**每批 = 一个 `js/pages/` 模块 + index.html 外壳中对应的一段路由/挂载**（或一个独立 HTML）。
一次只动一个模块。顺序叶子优先，`app.js` 外壳与 `admin.html/admin.js` 独立站放最后。

### 5.2 每批闭环

1. 读 legacy 源 + 《盘点报告》该模块功能要点清单。
2. Vue 3 `<script setup>` + @apple-ui 重建，行为等价；i18n 迁移时注意 `t(key)` 失配返回 key 本身的陷阱，
   用显式 key 清单核对而非 `||` 兜底；onclick 不用 JSON.stringify 的既有写法一并保持等价。
3. 验证四连：
   - `npm run build` 通过；
   - `npm run lint` 本模块零告警、无豁免；
   - **Playwright 截图对比**：无头登录基线（Gate 0 拍板，见下）拿到登录态，对迁移前后关键交互
     （列表/表单/弹窗/跳转/明暗切换）截图；
   - 功能清单逐项自检。
4. 无差异 → 删除 legacy 对应源 + 更新《迁移清单.md》→ `git commit -m "migrate: <模块名>"`（**只 commit 不 push**）。
5. 发现行为差异 → 停，报告差异点等主人裁决，不顺手修。

**无头登录基线**（Gate 0 拍板，二选一）：
- A（推荐）：后端已有测试账号体系则用固定测试账号走真实登录流，Playwright 存 session 复用；
- B：后端加一个仅本地环境生效的 mock 登录开关（涉及后端，需主人单独批）。

### 5.3 进度仪表盘

- 进度 = `find src/api/frontend/legacy -type f | wc -l` 单调递减到 0。
- 《迁移清单.md》每批更新：模块 / 批次 / commit hash / 验收状态 / 截图留存路径。

### Gate 2（每批）

主人打开页面抽查 + 看 commit diff，确认无逻辑改动后放行下一批。

---

## 阶段 3：收尾收敛

1. legacy/ 清零校验（永不迁移清单除外）；全仓 grep 无残留互相引用。
2. 清理孤儿资源：仅本次迁移产生的无用文件；他人文件一律不碰。
3. 删 legacy lint 豁免，全仓 lint 清零。
4. README 更新：目录结构、开发规范、新页面流程（= 组件库 + tokens，不写裸样式）。
5. 最终验收：全量 build + lint 零告警；路由冒烟遍历；风格一致性抽查
   （随机 10 处色板/间距/圆角对比 tokens，明暗各一遍）；删除 §4.2 的临时挂载。
6. 打 tag、交付《重构总结.md》（迁移明细、剩余事项、遗留风险）。

---

## 工作协议（全程有效）

**必须**：一次一个模块；验证过才提交；每批独立 commit + 更新清单；引用关系不明先查依赖图再问主人；CSS 压缩正则必须非贪婪并核对匹配长度；绝不能先 open(w) 再读文件。

**禁止**：顺手优化业务逻辑/接口/数据结构（坏味道只报告）；一次多模块；批量重构未迁移老代码；删未迁完的 legacy 文件；动后端（除已批准的挂载项）；绕过验证；CSS 用 `!important` 压 JS inline style（用 data-* 方案）。

**回滚**：出错 = `git revert <该批 commit>`，报告原因，小步重来。

---

## 风险与对策（新增/修订 v1）

| 风险 | 对策 |
|---|---|
| 无头截图拿不到登录态 | 阶段 0 定登录基线方案，Gate 0 拍板后才进阶段 2 |
| 新旧并存缓存打架 | Vue 用文件指纹；legacy 侧维持 `?v=` bump 纪律直至清零 |
| i18n `t()` 失配返回 key 本身 | 迁移时用显式 key 清单核对，禁 `||` 兜底 |
| FOUC 主题闪烁回归 | 等价迁移 localStorage + head 内联逻辑，列入行为等价清单 |
| CSS 压缩/写入事故（历史上出过 15k 规则误删、空文件提交） | 压缩正则非贪婪+核对匹配长度；写文件先读后写；每批 build 后截图验证 |
| 分支被其他会话切走 | 每批开工前 `git branch --show-current` 确认在 weekly-clean-mys |
| 误删永不迁移资产 | §0.2-8 清单写入盘点报告，阶段 3 按合法保留校验 |
| 子代理写长文超时 | 产出文档由主代理分段写或亲自写 |
