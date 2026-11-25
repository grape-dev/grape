# Plan — Issue #4372 分支选择框前缀搜索

## 方案

1. 新增纯函数模块 `src/api/frontend/app/src/lib/branchFilter.js`，导出 `filterBranchesByPrefix(list, keyword)` 与 `reconcileBranchSelection(selected, list)`：
   - keyword 去首尾空白并小写化；空串返回全部；
   - 分支名小写后 `startsWith(prefix)`，忽略大小写；
   - 非数组/缺 name 的坏数据跳过，不抛错；
   - 对账选中值：选中项不在可见列表时回退首个可见项，列表为空则清空，保证所见即所提交。
2. `src/api/frontend/app/src/views/DashboardView.vue`：
   - 引入 `filterBranchesByPrefix`；
   - 新增 `existingBranchSearch` / `newBranchRefSearch` 两个 `ref`；
   - 新增 `filteredBranches` / `filteredNewBranchRefs` 两个 `computed`；
   - 详情页「高级选项」在已有分支 select 上方增加搜索输入框，选项改为 `filteredBranches`；
   - 「新建分支」面板增加参考分支搜索框，选项改为 `filteredNewBranchRefs`；
   - 有分支但过滤为空时渲染 disabled 的「无匹配分支」选项；
   - `watch` 选项集/选中值，过滤后同步对账 `advWorkBranch` / `newBranchRef`，避免提交被隐藏旧值；无基准时阻止创建并提示；
   - 创建面板与详情面板共用同一套搜索与过滤逻辑，避免同一交互两套行为；
   - 搜索框 `type="search"` + `aria-label` + 清除按钮，对齐既有场景搜索；
   - `loadBranches` 刷新与新建成功后清空搜索词，保证新建后列表回显。
3. i18n：`dashboard.branch_search_placeholder`、`dashboard.branch_no_match` 中英文补全，并同步视图内兜底 map。
4. 样式：搜索框并入输入框分组并改用 tokens（间距/圆角/字号），`.adv-row` / `.dbv-newbranch` 允许换行并补窄屏媒体查询。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 匹配策略 | 前缀匹配、忽略大小写 | 需求明确前缀即可，忽略大小写更符合直觉 |
| 过滤位置 | 前端本地 computed | 分支列表已一次性加载，无新增接口 |
| 空态承载 | disabled option + 选中值对账 | 原生 select 无空态插槽；过滤后对账选中值，避免显示与提交值不一致 |

## 影响面

- `src/api/frontend/app/src/lib/branchFilter.js`（新增）
- `src/api/frontend/app/src/views/DashboardView.vue`（模板/状态/样式）
- `src/api/frontend/shell/locales/{zh,en}/dashboard.json`（词条）
- `tests/js/test_issue_4372_branch_prefix_filter.js`（新增）
- `docs/issue-4372/{spec,plan,task}.md`