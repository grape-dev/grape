# Plan — Issue #4372 分支选择框前缀搜索

## 方案

1. 新增纯函数模块 `src/api/frontend/app/src/lib/branchFilter.js`，导出 `filterBranchesByPrefix(list, keyword)`：
   - keyword 去首尾空白并小写化；空串返回全部；
   - 分支名小写后 `startsWith(prefix)`，忽略大小写；
   - 非数组/缺 name 的坏数据跳过，不抛错。
2. `src/api/frontend/app/src/views/DashboardView.vue`：
   - 引入 `filterBranchesByPrefix`；
   - 新增 `existingBranchSearch` / `newBranchRefSearch` 两个 `ref`；
   - 新增 `filteredBranches` / `filteredNewBranchRefs` 两个 `computed`；
   - 详情页「高级选项」在已有分支 select 上方增加搜索输入框，选项改为 `filteredBranches`；
   - 「新建分支」面板增加参考分支搜索框，选项改为 `filteredNewBranchRefs`；
   - 有分支但过滤为空时渲染 disabled 的「无匹配分支」选项；
   - `loadBranches` 刷新与新建成功后清空搜索词，保证新建后列表回显。
3. i18n：`dashboard.branch_search_placeholder`、`dashboard.branch_no_match` 中英文补全，并同步视图内兜底 map。
4. 样式：新增 `.dbv-branch-search` / `.dbv-nb-search`。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 匹配策略 | 前缀匹配、忽略大小写 | 需求明确前缀即可，忽略大小写更符合直觉 |
| 过滤位置 | 前端本地 computed | 分支列表已一次性加载，无新增接口 |
| 空态承载 | disabled option | 原生 select 无空态插槽，disabled 选项避免空下拉且不改变选中值 |

## 影响面

- `src/api/frontend/app/src/lib/branchFilter.js`（新增）
- `src/api/frontend/app/src/views/DashboardView.vue`（模板/状态/样式）
- `src/api/frontend/shell/locales/{zh,en}/dashboard.json`（词条）
- `tests/js/test_issue_4372_branch_prefix_filter.js`（新增）
- `docs/issue-4372/{spec,plan,task}.md`