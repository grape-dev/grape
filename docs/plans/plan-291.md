# Plan: Issue #291 — 帮助中心 API 文档优化

## 任务分解

1. `src/help_page_gen.py` 数据层：
   - 新增 `_assign_doc_numbers(items, section)`：usage/reference `1..N`；api 的 README 编号空（总览）、其余 `1..17`；
   - 新增 `_assign_toc_numbers(item)`：按 toc 层级计算层级编号（`1.1`/`1.1.1`），带文档前缀；
   - `_section_data` / `_reference_data` 调用赋值，将 `no` 写入 doc 与 toc 记录。
2. `src/help_page_gen.py` _JS_APP 侧边栏：
   - `sideList` 渲染编号 + 标题，有子节的文档渲染 caret + `.subnav`（默认收起、当前文档展开）；
   - 增加 caret 折叠/展开 click 处理。
3. `src/help_page_gen.py` _JS_APP 右侧标准化：
   - api section 内容后处理 `decorateApi`：按 h2 切分 `.ep-card`，解析方法/路径/描述，加标签（接口说明/请求参数/响应示例/错误码）。
4. `src/help_page_gen.py` _CSS：
   - `.dn.no` / `.subnav` / `.caret`；`.ep-card` / `.ep-method`（方法着色） / `.ep-path` / `.ep-title` / `.ep-label`；
   - 响应式（小屏徽章换行、抽屉交互保持）。

## 测试

- `tests/test_help_page_generation.py`：
  - 目录编号断言（api `no` 0/1..17、usage `no` 1..N）；
  - toc 层级编号断言（存在 `1.1`/`1.1.1`）；
  - 接口标准化 JS 标记断言（`.ep-card`、`.ep-method`、`.ep-label` 存在）；
  - 现有用例回归。

## 回归范围

- `tests/test_help_directory.py`、`tests/test_help_page_generation.py`、`tests/test_api_docs_sync.py`。
- 基线既有失败（help/api 与 docs/openapi 字节漂移 10 例）与本次改动无关，stash/对照确认。

## 风险

- 侧边栏渲染改动需保证桌面/移动端抽屉均可正常折叠；用现有 `.side.open` 抽屉逻辑，不引入新交互范式。
- 端点标题解析用正则兜底：无法解析出方法/路径的 H2（如 00-auth 的「认证方式…」）保持普通节标题样式，不回退异常。