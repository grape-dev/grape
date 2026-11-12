# Spec：可观测性（驾驶舱）页「找不到你想看的内容？」反馈入口（Issue #1916）

## 目标

在可观测性页（`renderObservability`）页头右侧、与页面标题同行，新增反馈入口：
文案「找不到你想看的内容？」+ 副文案「告诉我们你想看什么」；点击弹轻量反馈面板，
复用 `POST /api/feedback` 落库（运营管理台现有反馈列表可见），自动附带页面/路由/用户/筛选上下文。

## 现有契约（已核实）

- `src/api/routes/feedback.py` `FeedbackBody` 已含 `category/title/description/page_url/repo_key/feedback_kind` 等字段 → **后端零改动**。
- `title` 落库后展示于管理台反馈列表，且列表已有 `page_url` 列 → 上下文用 `title` + `page_url` 承载。
- 提交侧前端语义：登录态 = `localStorage.getItem('token')`；当前用户 = `localStorage.getItem('user')`（`username/nickname`）；当前仓库 = `app.currentRepo()`（`fullName`）。
- i18n：`I18N.t('observability.xxx')` 走 `locales/{zh,en}/observability.json`；改 locales 需 bump `index.html` 的 `__I18N_CACHE_VER__`；改 `observability.js` 需 bump 其 `?v=`。

## UI/交互

1. **入口按钮**（页头右侧，`<h2>` 同行）：
   - 主文案 + 副文案两行小按钮，样式复用 `.bt bt-o bt-s`（与页头现有按钮一致）；
   - `@media (max-width:768px)` 下收为纯图标按钮（💎），带 `aria-label`/`title`，不遮挡页头控件。
2. **轻量面板**（overlay+dialog，样式对齐 `feedback.js` 弹窗：rgba 遮罩/圆角 16px）：
   - 类型单选 3 项（缺指标 `missing_metric` / 想看某类内容 `want_content` / 其他 `other`），默认「缺指标」，样式复用 `.fb-cat-btn` 视觉（inline style 实现，不污染全局 CSS）；
   - 多行 textarea，`maxlength=500` + 实时字数 `n/500`；空内容提交按钮 disabled；
   - 提交中按钮 disabled + `...`，防重复提交；
   - 成功 → toast「已收到反馈，感谢！」并关闭面板；失败 → toast 错误、保留已填内容可重试。
3. **登录门**：未登录（无 token）入口不渲染；面板打开时再次校验，无 token → toast 登录提示（`app.showToast`）并 return。

## 上下文拼装（`page_url`）

```
hash 路由（#observability/<section>）?view=observability&section=<s>&tab=<t>&range=<r>&repo=<fullName>&user=<username>
```

- 只拼**非空**字段，`encodeURIComponent`；下拉/搜索等子筛选只在 inspect 子 Tab 存在，仅拼 `section/tab/range/repo/user`（Spec 口径：只拼必要字段，控制长度）。
- 总长截断到 500 字符（保护 page_url 列）。
- `title` = I18N「驾驶舱反馈：找不到想看的内容」（运营列表直接可见页面标识）。
- `description` = 用户输入（≤500，去首尾空白）。

## 提交与打点

- `API.post('/feedback', {category:'suggestion', title, description, page_url, repo_key, feedback_kind})`。
- 打点函数 `_obsFbTrack(name, attrs)`：优先 `window.__faro.api.pushEvent('obs_feedback', {...})`，SDK 缺失时降级 `console.info`（不抛错、不打断业务）。
- 事件：`expose`（入口渲染一次/会话）、`click`（打开面板）、`submit_ok` / `submit_fail`（含 kind、section、错误 reason）；`submit_fail` 额外 `console.error` 记录失败原因。

## 不做

- 不改 `/api/dashboard/ops/*` 数据接口与口径；不改 `feedback.py`；不新建后端；不做分类派单闭环；不改全局反馈弹窗行为。

## 验收标准（对齐 Issue）

- [ ] 驾驶舱首屏入口可见，样式与页头按钮一致；窄屏图标化不遮挡
- [ ] 提交后运营管理台反馈列表可见（title 含页面标识，用户/仓库随鉴权落库）
- [ ] 空内容禁用、500 字上限、提交中防重；失败保留内容重试
- [ ] 未登录入口不可见；无权限（接口 401/403）→ toast 报错，不丢内容
- [ ] 上下文自动附带（路由+筛选 range+用户+仓库）
- [ ] 打点：曝光/点击/成功/失败；失败日志
- [ ] 前端测试通过（node tests/js）；不改驾驶舱数据接口与口径
