# Plan: Issue #534 实现方案

## 根因
`get_scene_detail` / `list_scenes` / `list_scenes_with_pipeline`（src/api/routes/scenes.py）缺省 repo 解析依赖 `app_current_repo()`——一个固定返回 `None` 的 placeholder → `repo_key=""` → repo 作用域场景（devops_pipeline_scene）查不到 → 404/空列表。会话级 token（`tk_`）在 `deps.get_current_user_or_api_key` 已返回绑定的 `repo_key`，但路由未消费。

## 改动
1. `src/api/routes/scenes.py` 新增 `_resolve_scene_repo_key(repo, user)` 统一解析：
   - 显式 `repo` 参数优先（`_resolve_repo_key` 归一化，行为不变）；
   - 缺省回落 1：会话级 token（`auth_type=session_token`）绑定的 `repo_key`；
   - 缺省回落 2：请求中间件 `repo_var` 上下文（query param / 路径推导）。
2. `get_scene_detail` / `list_scenes` / `list_scenes_with_pipeline` 接入该解析（替换 `app_current_repo()` placeholder 调用）。
3. `get_scene_detail` 404 message 追加「跨仓库场景请附 --repo <owner/repo> 后重试」。
4. `tests/test_issue_534_scenes_session_repo_scope.py`：TestClient + dependency override 回归。

## 权限不放松
解析出的 repo_key 仍作为 `get_pipeline_config` / `get_scenes` 的 repo 作用域入参，走既有归属校验；显式 repo 指向无该场景的仓库时不被会话仓库兜底误命中（仍 404）。

## scenes list WORKER 模式 token scope 口径结论（#529 联动）
- 现状：`GET /api/scenes` 走 `require_no_session_token`（token-scope 分类 Phase：账户/平台管理类 user_only），`tk_` 会话 token 调用返回 403。
- 结论：**维持 user_only 不放开**（本 Issue 口径）。理由：
  1. scenes list 是跨仓库枚举入口（可指定任意 `--repo`），`tk_` 是 agent 任务执行凭据，放开等于允许会话凭据枚举任意仓库场景清单，超出「该仓库执行期凭据」的 scope 模型；
  2. Agent 侧实际诉求是「取当前场景 nodes」，已由 scenes detail 修复（本 PR）覆盖——`scenes detail --scene_id $GRAPE_SCENE_ID` 无需 --repo 即可命中；
  3. 若 #529 评估后确需放开，应按 `verify_session_token_for_repo` 模式做 repo 作用域强校验（仅返回绑定仓库的场景），属 #529 范围，本 Issue 不代做。
- 文档指引：WORKER Agent 需要场景清单时应使用 `grape_` API Key 或 Web JWT；会话内取当前场景用 `scenes detail --scene_id`。

## 与 #533 的关系
#533（同根因：注入 GRAPE_SCENE_ID 调 scenes detail 404）已有独立分支 feat/issue-533 实现同一修复（其 Spec 注明「服务端部分以 #534 Spec 为基准合并交付」）。本 PR 按 #534 Spec 独立实现并扩展到 scenes list 家族，两者合入先后者为幂等同款改动，无行为分歧。

## 不改动
- `app_current_repo` placeholder 保留（其余调用方行为不变）。
- runbook 技能资产不在本仓库（#530）。
