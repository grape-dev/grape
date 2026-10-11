# Plan: Issue #290

1. 修改 `src/api/static/js/pages/issues.js`（两处，原子变更）：
   - L363 `_openBoardDetail` 头部按钮：onclick URL 去 `t.is_pr` 分支统一 `'/issues/' + t.issue_number`；文案统一 `I18N.t('issues.detail.view_issue')`。
   - L976 `_render` 列表详情面板按钮：同上（变量 `_taskSel`）。
2. 验证 `locales/{zh,en}/issues.json` 的 `issues.detail.view_issue` 词条存在且语义正确（预期零改动）。
3. 新增 `tests/js/test_issue_290_view_issue.js` 回归测试：
   - 源码断言：两处按钮 URL 不含 `is_pr` 分支、统一 `/issues/`、文案走 i18n；
   - 行为断言：`_vcsUrl` mock 下 gitcode/github 两 provider 链接生成正确；
   - 负向断言：`_openPRDetail` 的 `/pull/` 按钮保持不变。
4. 运行 `node tests/js/test_issue_290_view_issue.js` + 既有 JS 测试抽样回归。
