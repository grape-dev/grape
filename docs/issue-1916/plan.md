# Plan：Issue #1916 驾驶舱反馈入口实现方案

## 切片与 TDD 顺序（一竖切片一测试一提交）

### 切片 1（test → feat）：上下文拼装 `_obsFeedbackPageUrl()`（纯函数 seam）
- 新增纯函数：hash + section/tab/range/repo/user → page_url（≤500 截断、只拼非空、encodeURIComponent）。
- **测试 seam**：`new Function` 沙箱驱动 `observability.js`，直接断言返回值（不依赖 DOM）。
- 测试用例：全字段、部分字段、空仓库/无用户、超长截断、query 顺序稳定。

### 切片 2（test → feat）：面板渲染 + 校验 + 提交防重 `_obsFb`（DOM seam）
- `openObsFeedback()` 打开面板；`_obsFbOnInput()` 字数/禁用联动；`_obsFbSubmit()` 提交。
- **测试 seam**：轻量 DOM 桩（getElementById 返回带 value/innerHTML/disabled 的元素），捕获 `API.post` 调用参数。
- 测试用例：
  - 未登录（无 token）`openObsFeedback` 直接 return 不渲染；
  - 空内容提交 → 按钮保持 disabled、无 API.post；
  - 正常提交 → API.post 参数含 `category=suggestion`、`feedback_kind`（默认 missing_metric）、`title`、`description`、`page_url`（切片 1 的产物）、`repo_key`；
  - 提交中二次点击 → 只发一次 API.post（防重）；
  - 失败 → 按钮恢复、内容保留；成功 → 面板移除。

### 切片 3（test → feat）：页头入口渲染 + 窄屏图标 + i18n + 打点
- `renderObservability` 页头右区渲染入口（登录才渲染）；窄屏由 CSS 断点收图标（`<span class="obs-fb-full">` 文案 + 图标 `<span class="obs-fb-ico">`，媒体查询隐藏切换）。
- locales zh/en 新增 `observability.fb_*` 键；index.html bump `__I18N_CACHE_VER__` 与 `observability.js?v=74→75`。
- 打点：expose/click/submit_ok/submit_fail。
- 测试：i18n 键 zh/en 全覆盖（沿 `test_observability_i18n.js` 的真实语言包断言法）；未登录时入口 HTML 不出现。

## 关键设计

- **不改 feedback.js**（只复用样式）；后端零改动。
- 面板 DOM id 前缀 `obsFb`（`obsFbOverlay/obsFbText/obsFbCount/obsFbSubmit/...`），与 `obsAlertModal` 等并存。
- 打点统一 `_obsFbTrack(name, attrs)`，faro 缺失降级 console，绝不抛错。
- 无权限语义：未登录隐藏入口；已登录但接口拒绝 → 走 `API.post` 既有错误路径（toast + 保留内容重试）。

## 风险与对策

- `renderObservability` 每次切换分区重渲染 → 曝光打点做**会话级去重**（`_obsFbExposed` 标志），避免刷屏。
- `index.html` 版本号漏 bump → 测试固化：断言 html 中 `observability.js?v=` 值与 locales 内容 hash 关联（简化：断言 v ≠ 74）。

## 提交拆分

1. `test: #1916 驾驶舱反馈 page_url 上下文拼装失败测试`
2. `feat: #1916 驾驶舱反馈 page_url 上下文拼装实现`
3. `test: #1916 驾驶舱反馈面板/校验/防重失败测试`
4. `feat: #1916 驾驶舱反馈面板 + 提交实现`
5. `feat: #1916 入口渲染/窄屏图标/i18n/打点 + 构建版本号`
