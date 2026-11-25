# 任务拆解: Issue #4230

- [x] 新增 `lib/plainTextSummary.js`（Markdown 剥离 + 首段 + 空白归一 + 超长截断）。
- [x] `SceneCard.vue` 描述改用摘要工具，title 保留不截断摘要，空值走 `tf('no_description')` 兜底。
- [x] 新增 zh/en `skills.no_description` 词条。
- [x] 新增 `tests/js/test_issue_4230_plain_text_summary.js`（34 项检查）。
- [x] 同步前端 chunk / all-views / i18n 缓存版本守卫。
- [x] 回归确认 `SceneMarket.vue` 详情页渲染不变。