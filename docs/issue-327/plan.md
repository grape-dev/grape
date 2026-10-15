# Plan: Issue #327 — 引擎列表渲染修复

## 改动清单
1. `runtimes.js` renderEngines 搜索 filter 修正：`rt.tags` → `rt.agent_tags`
2. `runtimes.js` renderEngines 卡片新增字段：类型(rt.name)、版本(rt.version)、安装状态(rt.installed)
3. `runtimes.js` renderEngines 所有字段统一 fallback `|| '—'`
4. 新增 `tests/js/test_issue_327_engine_list_render.js`

## 验证
- node tests/js/test_issue_327_engine_list_render.js
- pytest 现有测试不受影响
