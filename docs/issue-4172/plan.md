# Issue #4172 方案设计（Plan）

## 1. 测试 seam

| 切片 | seam | 说明 |
|------|------|------|
| 操作区顺序 | `ReposView.vue` 模板源码断言 | 与 `test_repos_host_provider.js` 一致，按源码级断言锁定 DOM 顺序 |
| 切换文案 | 模板 `onlyHosted ? ... : ...` 三元表达式 | 断言标签与 tooltip 都指向目标视图 |
| 切换行为 | `toggleScope()` / `showAllRepos()` | 断言切换 `onlyHosted` 并重置分页 |
| 空态引导 | `showHostedEmpty` 计算属性 | 仅无已托管且存在未托管候选时展示可点击引导 |
| 文案同步 | `shell/locales/{zh,en}/repos.json` | 解析 JSON 断言 key/文案 |

## 2. 模板改动（`ReposView.vue`）

1. `project-section-header-right` 内按钮重排：范围切换、刷新、搜索框、新建项目。
2. 范围切换：
   - 标签 `onlyHosted ? t('repos.lobby.scope_all') : t('repos.lobby.scope_hosted')`；
   - tooltip `onlyHosted ? t('repos.lobby.scope_all_title') : t('repos.lobby.scope_hosted_title')`；
   - 点击调用 `toggleScope()`。
3. 空态：
   - 由 `showHostedEmpty` 决定标题/图标；
   - 可点击引导用 `<button class="lobby-empty-link" @click="showAllRepos">{{ t('repos.lobby.scope_all') }}</button>`，
     文案拆为 `empty_hosted_desc_prefix` + 可点击的「全部仓库」+ `empty_hosted_desc_suffix`；
   - 非引导场景回落到通用 `empty_title` / `empty_desc`。

## 3. 脚本改动

- 新增 `hasUnhostedRepos = computed(() => repos.value.some((r) => !r.is_hosted))`。
- 新增 `showHostedEmpty = computed(() => onlyHosted && hostedCount === 0 && hasUnhostedRepos)`，
  保证全部仓库为 0 条时不展示托管引导。
- 新增 `toggleScope()` 与 `showAllRepos()`，两者都重置 `repoPage`。

## 4. locale 改动

| key | zh | en |
|-----|----|----|
| `scope_all_title` | 点击查看全部仓库（含未托管） | Click to view all repositories (including unhosted) |
| `scope_hosted_title` | 点击只看已托管仓库 | Click to show hosted repositories only |
| `empty_hosted_desc_prefix` | 点右上角「 | Use " |
| `empty_hosted_desc_suffix` | 」浏览你的仓库并托管一个。 | " (top right) to browse and host one. |

移除不再使用的 `empty_hosted_desc`。

## 5. 验证

```bash
node tests/js/test_issue_4172_repos_scope_ui.js
node tests/js/test_repos_host_provider.js
node tests/js/test_issue_372_r13_ui_optimization.js
node tests/js/test_i18n_repos_local_refresh.js
```