# Task: Issue #534 任务拆解

- [x] T1 根因定位：scenes detail/list 缺省 repo 解析失效（app_current_repo placeholder 固定返回 None）
- [x] T2 服务端修复：_resolve_scene_repo_key 统一解析（显式 repo > 会话 token repo_key > repo_var），接入 detail / list / with-pipeline
- [x] T3 404 提示：detail 报错信息附 --repo 变通指引
- [x] T4 scenes list WORKER token scope 口径结论（plan.md §4，#529 联动，维持 user_only + 文档指引）
- [x] T5 回归测试：无 --repo 命中会话所属场景 / 显式 --repo 优先 / 不存在 scene_id 404 含提示 / list 缺省上下文解析
- [x] T6 全量相关测试通过
- [x] T7 自审查 + 敏感增量扫描 + 提交 + PR（base=weekly-08-mys）
