# Plan: Issue #533 实现方案

## 根因
`get_scene_detail`（src/api/routes/scenes.py）原依赖 `app_current_repo()`（固定返回 None 的 placeholder）解析仓库 → `repo_key=""` → repo 作用域场景查不到 → 404。CLI `grape scenes detail --repo owner/repo` 经 query param + 请求中间件（`repo_var`）可命中，但路由本身未消费该参数。

## 改动
1. `src/api/routes/scenes.py` `get_scene_detail`：
   - 新增 `repo: str = ""` query 参数（显式优先，`_resolve_repo_key` 归一化）；
   - 缺省回落 1：会话级 token 的 `user["repo_key"]`（`deps.get_current_user_or_api_key` 已返回）；
   - 缺省回落 2：请求中间件 `repo_var` 上下文；
   - 404 message 追加「跨仓库场景请附 --repo <owner/repo> 后重试」。
2. `tests/test_issue_533_scenes_detail_repo_scope.py`：TestClient + dependency override 模拟 tk_ 会话用户，四类回归（见 spec.md）。

## 不改动
- `app_current_repo` placeholder 保留（无其他调用方依赖其行为变化）。
- runbook 技能资产不在本仓库，归 #530。
