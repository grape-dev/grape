# Spec: Issue #4230 — 场景卡片 Markdown 原样露出

> 来源：Issue #4230 评论（id 已脱敏，Issue Agent 产出，评级 ready）。实现基线 `weekly-mys`。

## 目标

场景市场卡片不再把 `market_description` 的 Markdown 标记原样露出，改为一段可读的纯文本摘要；详情页保持不变。

## 范围

- `src/api/frontend/app/src/views/SceneCard.vue`：描述改用摘要工具，空描述显示本地化兜底文案。
- 新增 `src/api/frontend/app/src/lib/plainTextSummary.js`：剥离 Markdown 标记、取首段、空白归一、超长截断。
- 词条 `src/api/frontend/shell/locales/{zh,en}/skills.json`：新增 `no_description`。
- `styles/global/app.css` 的 2 行 CSS 截断保持不变。

## 非目标

- 不改场景详情页 `SceneMarket.vue` 的纯文本渲染。
- 不引入第三方 Markdown 库，不直接 `v-html` 渲染原始字段。
- 不改后端字段与接口。

## 验收

- 含 `#`/`##`/`**`/有序列表的描述不再出现这些标记。
- 摘要为首个可读段落，不出现半句或成串列表符。
- 空/`null` 描述显示兜底文案，卡片高度稳定。
- 摘要工具单测覆盖标题/列表/强调/链接/代码/空值/超长；详情页回归不变。