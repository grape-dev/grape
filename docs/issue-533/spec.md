# Spec: Issue #533 — scenes detail 缺省查询按会话仓库解析（404 修复 + 降级指引）

> 来源：Issue Agent Spec（评论 id=197021491）| 与 #534 同根因，服务端部分以 #534 Spec 为基准合并交付

## 目标
- `GET /api/scenes/{scene_id}`（CLI `grape scenes detail`）未显式传 `repo` 时，按会话上下文解析场景归属：
  - 会话级临时 token（`tk_`）→ 取其绑定的 `repo_key`；
  - 否则回落请求中间件解析的 repo 上下文（query param / 路径推导，`src/api/main.py` `repo_var`）；
  - 显式 `--repo` 优先，行为不变（向后兼容）。
- 404 报错信息补充「跨仓库场景请附 --repo <owner/repo> 后重试」变通提示，保留 404 契约。
- 解析出的 repo_key 仍走 `get_pipeline_config` 既有 repo 作用域归属校验，权限不放松。

## 范围
- In：`src/api/routes/scenes.py` `get_scene_detail`；`tests/test_issue_533_scenes_detail_repo_scope.py` 四类回归。
- Out：issue-runbook 技能资产改动（#530 文档侧对齐，资产不在本仓库）；scenes list token scope（#529）。

## 验收标准
- [x] 无 `--repo` 时会话所属场景命中（不再 404）
- [x] 显式 `--repo` 优先：指向无该场景的仓库仍 404
- [x] 不存在 scene_id 仍 404，报错附 `--repo` 提示
- [x] 降级指引下 A2A 目标判定不失真（命中 → nodes 完整 any() 可判；404 → 视同未绑定走默认路由）
