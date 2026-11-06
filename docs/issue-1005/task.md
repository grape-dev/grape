# Task: Issue #1005

- [x] T1 复核 weekly-mys 已含 #640（映射 + 回读校验），确认剩余差距为 id 口径与错误语义。
- [x] T2 milestones 列表/创建/更新补 `id`（url 尾部数字），number 兼容保留。
- [x] T3 issues update 的 milestone_id 解析（id 优先、number 兜底），以解析出的 number 写上游。
- [x] T4 失败语义分流：不存在 400 / 无权限 403 / 上游忽略 422，移除回读不一致的 502。
- [x] T5 issues get 回显 milestone 并补 id（QA 可断言）。
- [x] T6 同步 OpenAPI 描述、help/api 与 docs/openapi 文档口径。
- [x] T7 新增/更新测试：解析、回读、错误分支、契约描述。
- [x] T8 自审查 + 敏感信息增量扫描 + 创建 PR + A2A QA。