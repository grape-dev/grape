# Plan: Issue #372

沿用 #358 已落地的 `KNOWLEDGE_MAX_CONTENT_BYTES` 环境变量配置体系，仅调整默认常量与报错文案，无架构变更。

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/api/routes/knowledge.py` | `_DEFAULT_MAX_CONTENT_BYTES = 5 * 1024 * 1024`；新增 `_CONTENT_LIMIT_CONFIG_HINT` 常量；创建/更新两处超限报错追加提示 |
| `src/api/routes/events.py` | add_knowledge 报错文案导入并追加同一提示常量（上限值经 `MAX_CONTENT_BYTES` 导入自动跟随） |
| `src/api/static/js/pages/knowledge.js` | 兜底 `contentLimitBytes` 102400→5242880、`contentLimitDisplay` '100KB'→'5MB' |
| `.env.example` | 注释与示例值 5242880=5MB |
| `tests/test_api_routes.py` | 默认值断言 102400→5242880；边界用例改为 100KB~5MB / >5MB；报错断言含 `KNOWLEDGE_MAX_CONTENT_BYTES` |
| `docs/issue-372/` | spec/plan/task + 默认值变更说明（部署/配置文档同步） |

## 风险与边界

- prompt 注入侧 `_KNOWLEDGE_INJECT_MAX_CHARS=8000` 截断保持不动，5MB 知识不会全量进入 LLM context。
- 仅放宽不收紧（100KB→5MB），存量知识读写不受影响。
- stats API 动态下发上限，前端兜底值仅在 stats 不可用时生效。

## 验证

`python -m pytest tests/test_api_routes.py -k "content or max_content" ` 全绿；全量回归现有测试。
