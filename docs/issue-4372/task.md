# Task — Issue #4372 分支选择框前缀搜索

- [x] T1 新增 `lib/branchFilter.js` 前缀过滤纯函数（忽略大小写、坏数据容错）。
- [x] T2 DashboardView 引入过滤函数，新增搜索状态与 computed。
- [x] T3 详情页「选择已有分支」下拉顶部增加搜索框，选项改为过滤结果。
- [x] T4 「新建分支」入口已有分支参考列表增加搜索框与过滤。
- [x] T5 无匹配时展示「无匹配分支」disabled 空态选项。
- [x] T6 刷新/新建成功后清空搜索词，保证新建后列表回显。
- [x] T7 补全中英文 i18n 词条与视图兜底 map，新增样式。
- [x] T8 新增 `tests/js/test_issue_4372_branch_prefix_filter.js`，覆盖命中/未命中/大小写与视图接线。