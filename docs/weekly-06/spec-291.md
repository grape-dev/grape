# Spec: Issue #291 — 帮助中心 API 文档优化

> 目标分支：weekly-06；来源：Issue Agent Spec + VOD 收敛需求。
> 实现层：前端 UI 展示层（`src/help_page_gen.py` 生成物 `src/api/static/help.html`）。

## 背景

帮助中心 API 文档页面（`/static/help.html`，由 `help/*.md` 经 `src/help_page_gen.py` 渲染）现状：

1. 左侧目录无序号，层级关系不直观，定位慢；
2. 目录各分组/小节命名与展示格式不统一；
3. 右侧接口介绍是纯 markdown 渲染，无标准字段结构（说明/方法/路径/参数/响应/错误码 无统一呈现）；
4. 移动端目录为抽屉，但无折叠/展开能力。

内容层（help/api 各篇 markdown 的七要素模板化）已完成（见 `docs/weekly-03/api-docs-standardization-design.md`），
本 Issue 聚焦**展示层**：让左侧目录可编号/可折叠，右侧接口展示标准化、与业界 API 文档（Swagger/Redoc/Stripe）一致。

## 需求

- 左侧目录：模块级编号 `1 / 2 / 3 …`，小节级层级编号 `1.1 / 1.2 / 1.1.1 …`；命名格式统一
  （编号前缀 + 标题）；分组支持折叠/展开。
- 右侧接口：每个端点以统一卡片呈现「接口说明 → 请求方法与路径（方法徽章 + 路径）→ 请求参数表 → 响应示例 →
  错误码说明」。
- 桌面端与移动端响应式适配，风格与帮助中心其他页面一致。
- 不改任何接口逻辑、不改 API 接口本身；不改 help/*.md 内容语义（仅展示层）。

## 验收标准（对应 Acceptance Criteria）

- [x] 左侧目录条目有序号/层级编号，可快速定位；
- [x] 目录各分组/条目命名格式统一（编号 + 标题）；
- [x] 右侧每个 API 接口含标准化字段展示（说明/方法/路径/参数/响应/错误码）；
- [x] 目录与接口展示风格与帮助中心其他页面一致；
- [x] 桌面端与移动端均可正常浏览（含目录折叠）；
- [x] 现有测试通过（help 相关回归），新增测试覆盖目录编号与接口字段标准化逻辑。

## 技术方案

### 数据层（help_page_gen.py，Python）

- `_assign_doc_numbers(items, section)`：为每个文档分配顺序编号
  - usage/reference：按文档顺序 `1..N`；
  - api：README（总览）编号为空（显示「总览」徽标），其余按顺序 `1..17`（与 README 目录一致）；
- `_assign_toc_numbers(item)`：为 doc.toc 每行按标题层级计算层级编号，并带文档级前缀
  （`1.1`、`1.2`、`1.2.1` 等）；toc 为同层兄弟的保持同级递增。

### 侧边栏渲染（_JS_APP，前端 JS）

- 目录项显示 `编号 + 标题`（`.dn` 带 `data-no`）；
- 每个有子小节的文档渲染可折叠 `.subnav`（子链接 `.ds` 带编号），默认收起、当前文档展开；
- caret（▸/▾）点击折叠/展开；移动端抽屉沿用现有 `.side.open` 逻辑。

### 右侧接口标准化（_JS_APP + _CSS）

- API section 内容后处理：按 `h2` 切分端点卡片 `.ep-card`；
- 解析 H2 标题中的 `METHOD /path — 描述`：
  - 方法徽章 `.ep-method`（GET/POST/PUT/DELETE/PATCH 等，按方法着色）；
  - 路径 `.ep-path`（等宽）；
  - 描述 `.ep-title`；
- 卡片内栏目标识：
  - `blockquote` → 「接口说明」标签（已有 tip/warn 卡片样式）；
  - `table` → 「请求参数」标签（含 字段/类型/必填/说明 列的视为参数表，其余默认标注）；
  - `pre code` → 「响应示例」标签；
  - 含「返回：」/「错误」的段落 → 「错误码」样式；
- CSS 统一端点卡片、标签、徽章配色，保持现有主题变量（深/浅色）一致；
- 响应式：小屏下徽章与标题换行、卡片内边距收缩，目录抽屉逻辑不变。

## 测试

- `tests/test_help_page_generation.py` 扩展：
  - 目录编号：api 文档 `no` = 0/1..17，usage 文档 `no` = 1..N；
  - toc 编号：存在形如 `1.1`、`1.1.1` 的编号且与层级对应；
  - 接口标准化：生成的 JS 含方法徽章/端点卡片逻辑（`.ep-card`/`.ep-method`），help-data 字段完整；
- 回归：`tests/test_help_directory.py`、`tests/test_api_docs_sync.py`。
- 已知基线问题：help/api 与 docs/openapi 的 10 例字节一致断言为基线既有漂移（不属本改动）。

## 影响的模块

- `src/help_page_gen.py`（数据编号 + _JS_APP 渲染 + _CSS 样式）→ 修改
- 生成物 `src/api/static/help.html`（.gitignore，启动期重生成）→ 受影响
- `tests/test_help_page_generation.py` → 新增用例
- `docs/weekly-06/spec-291.md`、`tasks/plan-291.md`、`tasks/todo-291.md` → 新增