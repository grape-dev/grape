# Task: Issue #926 — 任务拆解

- [x] T1 后端：list_repo_issues 补 q/statuses 参数（SQL 参数化）
- [x] T2 后端：PR 伪卡片并入统一 LIMIT/OFFSET 分页（created_at/状态优先级合并排序，total 计入 PR）
- [x] T3 后端：分页模式 attachments 瘦身（count 替代清单）；per_page=0 兼容保持
- [x] T4 前端：loadTasks 分页请求 + sentinel + IntersectionObserver + 按 id 去重追加
- [x] T5 前端：tab/筛选/搜索/排序重置第 1 屏、选中保留；自动刷新按 id 合并
- [x] T6 前端：深链未命中走详情接口直达；时间线最近 20 条 + 加载更多
- [x] T7 测试：新增 tests/test_issue_926_paged_loading.py；修正 test_issues_api.py 旧断言
- [x] T8 自审查 + 建 PR（base=weekly-mys）+ A2A 通知 QA
