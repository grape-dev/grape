# Plan: Issue #926 — 实现方案

## 后端（src/api/routes/issues.py · list_repo_issues）
1. 新增 `q`（标题/编号 LIKE）与 `statuses`（逗号分隔 issue.status 多选）服务端过滤，SQL 参数化。
2. 分页口径修正（per_page>0）：PR 伪卡片与 issue 行按同一排序键（mode=default 为状态优先级 + created_at；mode=time 为 created_at）合并后再统一 LIMIT/OFFSET 切页：
   - total = COUNT(issue) + len(pr_items)（PR 卡片计入 total，滚动累计一致）
   - issue 行窗口取 offset+per_page+pr_count 条，与全量 PR 卡合并排序后切 [offset, offset+per_page)
   - PR 卡片同样受 q/statuses 过滤
3. 分页模式响应瘦身：attachments 只回 `attachments_count`（attachments=[]），事件聚合仍按返回集合收窄；per_page=0 旧模式保持全量清单兼容。

## 前端（src/api/static/js/pages/issues.js）
1. loadTasks 改分页请求（repo_all 视图）：`per_page=30&page=N&q=&statuses=&creators=&sort_order&mode`；mine 视图保持全量。
2. IntersectionObserver + sentinel：触底且 has_more 时 page+1 追加；按 id 去重合并（更新已有 + 追加新项），滚动中不整表重排。
3. 状态重置：切 tab/筛选/搜索/排序/换仓 → 重置第 1 屏重新拉取，保留 _taskSel 选中。
4. 深链：列表未命中时按 `/issues/{owner}/{repo}/{number}` 详情接口构造任务对象直达详情。
5. 自动刷新（_startScenePoll）：按 id 合并第 1 屏更新，不整表替换。
6. 详情分块：已有基础卡 0 请求骨架 + /pipeline、/attachments 异步填充；事件时间线最近 20 条 + 「加载更多」。
7. 前端保留状态/搜索二次过滤（与服务端口径一致），确保旧后端兼容。

## 测试
- 后端：tests/test_issue_926_paged_loading.py（分页合并口径、q/statuses 过滤、attachments 瘦身、per_page=0 兼容）；更新 test_issues_api.py 旧 PR 卡片分页断言为新口径。
- 前端：交互验收按 Spec 交 qa/sre 环节验证（编码阶段不做长驻前台运行）。

## 风险
- 分页口径变更影响既有调用方 → per_page=0 保持兼容 + 单测覆盖边界。
- 前端切片逻辑大量替换 → 渐进替换，保留二次过滤与既有 tab/搜索行为。
