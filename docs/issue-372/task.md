# Task: Issue #372

- [x] knowledge.py：默认常量 5MB + 报错文案追加配置提示（创建/更新两处）
- [x] events.py：agent 写入路径报错文案同步（共用 `_CONTENT_LIMIT_CONFIG_HINT`）
- [x] knowledge.js：前端兜底值 5MB
- [x] .env.example：注释与示例值 5242880=5MB
- [x] docs/issue-372/：spec / plan / task / 默认值变更说明
- [x] tests/test_api_routes.py：默认值、边界（100KB~5MB 可入库 / >5MB 拒绝）、报错含配置方法断言
- [x] 自审查（五轴）+ 建 PR + Issue 评论 + A2A QA
