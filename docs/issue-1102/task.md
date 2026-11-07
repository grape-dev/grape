# Task: Issue #1102

| # | 任务 | 产物 | 状态 |
|---|------|------|------|
| T1 | RED：tests/js/test_issue_1102_load_more.js（i18n 键、默认最新 20、onclick stopPropagation、向更早展开、按钮隐藏、详情重置、版本号 bump） | tests/js/test_issue_1102_load_more.js | done |
| T2 | GREEN：weekly-10 事件历史分批块修正方向（slice(0,_shown)）+ 按钮置底 stopPropagation + `_loadMorePipeEvents` 本地展开 + `_openBoardDetail` 重置 | src/api/static/js/pages/issues.js | done |
| T3 | zh/en tasks.json 新增 `load_earlier_events` 键 | src/api/static/locales/{zh,en}/tasks.json | done |
| T4 | index.html issues.js?v=132 → 133 | src/api/static/index.html | done |
| T5 | 回归：test_issue_539 / 342 / 329 js 测试 + i18n 键校验 | node tests/js/* | done |

## 验收命令

```bash
node tests/js/test_issue_1102_load_more.js   # 全部断言通过
node tests/js/test_issue_539_review_status_render.js   # 回归
python3 scripts/check-i18n-keys.py           # zh/en 键集合一致
```
