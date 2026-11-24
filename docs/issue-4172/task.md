# Issue #4172 任务拆解（Task）

| # | 切片 | 交付 |
|---|------|------|
| 1 | 操作区按钮重排 | `ReposView.vue`：范围切换 → 刷新 → 搜索框 → 新建项目 |
| 2 | 范围切换文案语义反转 + tooltip 描述点击效果 | `ReposView.vue`：`toggleScope()` + 目标视图文案 |
| 3 | 空态「全部仓库」可点击切换 | `ReposView.vue`：`showHostedEmpty` + `showAllRepos()` + `.lobby-empty-link` 样式 |
| 4 | zh/en locale 同步 | `shell/locales/{zh,en}/repos.json`：tooltip 与空态分段文案 |
| 5 | 回归测试 | `tests/js/test_issue_4172_repos_scope_ui.js` 覆盖顺序、文案、切换、空态、双语 |
| 6 | 文档 | `docs/issue-4172/{spec,plan,task}.md` |

## 验证命令

```bash
node tests/js/test_issue_4172_repos_scope_ui.js
node tests/js/test_repos_host_provider.js
node tests/js/test_issue_372_r13_ui_optimization.js
node tests/js/test_i18n_repos_local_refresh.js
```

> 说明：`test_repos_host_provider.js` 在当前基线已失败（断言旧 `HOSTS` 常量，现实现走
> `window.platformHost`），与本次改动无关；本次新增用例与 #372、i18n 套件均通过。