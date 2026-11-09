# Spec: Issue #534 — scenes detail/list 缺省按会话上下文解析场景归属

> 来源：Issue Agent Spec（Issue #534 评论）| 基线分支 weekly-08-mys

## 目标
- `GET /api/scenes/{scene_id}`（CLI `grape scenes detail`）未显式传 `repo` 时，按会话上下文解析场景归属：
  - 会话级临时 token（`tk_`）→ 取其绑定的 `repo_key`；
  - 否则回落请求中间件解析的 repo 上下文（query param / 路径推导，`src/api/main.py` `repo_var`）；
  - 显式 `--repo` 优先，行为不变（向后兼容）。
- `GET /api/scenes` / `GET /api/scenes/with-pipeline`（scenes list 家族）同样替换失效的 `app_current_repo()` placeholder（固定返回 None），改用统一解析。
- 404 报错信息补充「跨仓库场景请附 --repo <owner/repo> 后重试」变通提示，保留 404 契约。
- scenes list WORKER 模式 token scope 口径结论输出（与 #529 联动，不阻塞本 Issue）。

## 范围
- In：`src/api/routes/scenes.py`（`get_scene_detail` / `list_scenes` / `list_scenes_with_pipeline` 的 repo 解析统一）；`tests/test_issue_534_scenes_session_repo_scope.py` 回归测试。
- Out：runbook/文档示例补 --repo（#530）；scenes list token scope 放开（#529 评估）；token scope 体系重构。

## 验收标准
- [ ] 无 `--repo` 时会话所属场景命中（不再 404）
- [ ] 显式 `--repo` 优先：指向无该场景的仓库仍 404
- [ ] 不存在 scene_id 仍 404，报错附 `--repo` 提示
- [ ] scenes list 缺省按会话/请求上下文解析 repo（替换 app_current_repo placeholder）
- [ ] scenes list WORKER token scope 口径结论输出（见 plan.md §4）
- [ ] 新增测试覆盖上述场景，现有测试全通过
