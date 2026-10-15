# Spec: Issue #327 — 引擎列表显示异常修复

## 问题
点击仓库下引擎入口显示有 2 个引擎条目，但未展示引擎名称、类型、运行状态、配置等关键字段。

## 根因分析
排查 `renderEngines` 函数（`src/api/static/js/pages/runtimes.js`）发现以下问题：

1. **搜索过滤字段映射错误**：filter 中使用 `rt.tags`（API 返回的 engine_capabilities 列表，元素为 dict），而非 `rt.agent_tags`（DB 中的字符串标签列表）。`.join(' ')` 对 dict 产生 `[object Object]`，搜索功能失效。
2. **列表卡片缺少关键字段**：卡片仅展示 name/health/scope/worker/skills/scene_tags/agent_tags/health_score，缺少「类型」(rt.name)、「版本」(rt.version)、「安装状态」(rt.installed) 等 Spec 要求的关键字段。
3. **字段缺失时无 fallback**：若 API 返回的 runtime 对象部分字段为 undefined/null（如 credential 缺失导致 worker_name 为空），卡片对应区域直接为空白，用户感知为「无具体信息」。

## 修复方案
1. 修正搜索 filter：`rt.tags` → `rt.agent_tags`
2. 引擎卡片新增「类型」「版本」「安装状态」字段显示
3. 所有字段统一 `|| '—'` fallback，确保不会出现空白区域
4. 新增 JS 测试覆盖渲染逻辑

## 影响范围
- `src/api/static/js/pages/runtimes.js`：`renderEngines` 函数
- `tests/js/test_issue_327_engine_list_render.js`：新增测试
