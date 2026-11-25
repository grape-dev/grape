# Issue #4233 任务拆解（Task）

| # | 切片 | 交付 |
|---|------|------|
| 1 | 第三分支 disabled 化 | `ReposView.vue`：去 click、加 `disabled` + `title` + 禁用类 |
| 2 | 禁用态样式 | `ReposView.vue` scoped：`cursor:not-allowed`、hover 无高亮 |
| 3 | 死代码清理 | 移除 `hostNoPermission()` |
| 4 | zh/en 词条 | `shell/locales/{zh,en}/repos.json`：新增 `no_permission_host_title` |
| 5 | 回归/功能测试 | `tests/js/test_issue_4233_repos_can_host_disabled.js` |
| 6 | 文档 | `docs/issue-4233/{spec,plan,task}.md` |

## 验证命令

```bash
node tests/js/test_issue_4233_repos_can_host_disabled.js
node tests/js/test_issue_4172_repos_scope_ui.js
node tests/js/test_i18n_repos_local_refresh.js
python3 -m json.tool src/api/frontend/shell/locales/zh/repos.json >/dev/null
python3 -m json.tool src/api/frontend/shell/locales/en/repos.json >/dev/null
```