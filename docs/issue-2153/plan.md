# Plan: Issue #2153 — 组织流水线端到端集成

## 测试 seam

- **HTTP seam**：`TestClient(app)` + `get_current_user_or_api_key` 依赖覆盖（JWT 用户），走真实路由与鉴权口径。
- **DB seam**：每用例把 `src.kernel.session_manager.db` 重绑到 `tmp_path` 下的独立 SQLite 文件，
  `init_database()` 建全量表；teardown 还原旧连接，避免污染其他套件。
- **VCS adapter seam（唯一 mock 范围）**：
  - `org_provision.create_new_repo` / `org_provision.host_repo` — 建仓 / 托管；
  - `user_db.get_gitcode_token` — bot token；
  - `issues_center._find_open_issue_by_title` / `_post_gitcode_issue` — 子 Issue 远端建单 / 查重；
  - `event_pipeline.run_pipeline` — worker 派发入口（不启真实 worker、不触网）。
- **保留真实**：`org_dispatch` 状态机、`_aggregate_dispatch`、`sweep_once`、`create_task_via_issue`
  建档逻辑、`org_db` 组织/关联/仓库 DB 行为。

## 走向

1. `OrgE2E` 夹具：建团队组织（`POST /api/orgs`）→ 同步 provision（VCS mock）到 `ready`。
2. 关联 2 个目标仓库（`POST /api/orgs/{id}/repos`，maintainer 校验 mock 放行）→ `GET .../repos` 断言可见。
3. 组织仓库建组织需求 Issue（`scene_id` = 组织场景）。
4. `POST /api/orgs/{id}/dispatches` targets=2 → 2 target `running`，子 Issue 与 target 一一对应。
5. happy path：完成 1 个子 Issue（真实完成迁移）+ 缩 timeout 让另 1 个 target 走真实超时失败 → patch `partial_failed`；
   retry 失败 target → 完成 → 聚合 `completed`。
6. 幂等：独立组织，终态后重复同 targets 下派 → 复用 batch，子 Issue 数量不变。
7. cancel：独立组织，下派后 `POST .../cancel` → 子 Issue 取消 + `dispatch=cancelled`。

## 风险

| 风险 | 缓解 |
|------|------|
| 重绑 db 污染其他套件 | teardown 还原 `sm.db` / `_db_path` 及所有被重绑模块的 `db` 引用；integration 默认不纳入主回归 |
| 子 Issue 完成/失败推进依赖真实 worker | 完成走 issue 完成迁移；失败走 sweeper 超时路径；worker 派发入口 mock |
| 场景预校验误拦 | 目标仓库场景节点为空集，预校验结构/节点检查均通过 |
