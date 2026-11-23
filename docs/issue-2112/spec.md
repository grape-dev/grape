# Issue #2112 需求文档：组织需求跨仓下派与子任务编排（MVP）

## 目标

组织需求（org Issue）一次下派到 N 个已关联仓库：每个目标仓库各生成 1 个子 Issue，
以内部服务身份 + 目标仓库 bot token 触发该仓库场景，并全程记录父子关系、幂等、取消、
单目标重试与状态汇总。

## 范围（In）

1. 两张表：`org_dispatch`、`org_dispatch_target`（幂等迁移）。
2. 4 类下派 API：创建、列表、详情、cancel、单目标 retry。
3. 子 Issue 建单复用 `src/api/issues_center.py::create_task_via_issue`，不复制建单逻辑。
4. 场景解析 4 档：请求 scene_id → `get_repo_scene` → 仓库唯一 active 场景 → failed(NO_TARGET_SCENE)。
5. 汇总 sweeper：60s 周期，读 `issue.status` / `issue.stage`，单目标 2h 超时 fail。
6. 取消逻辑抽 `cancel_issue_core`，路由与下派共用。
7. 6 类审计事件登记：`dispatch_created` / `target_created` / `dispatch_completed` /
   `dispatch_partial_failed` / `dispatch_cancelled` / `target_retried`。

## 范围（Out）

- `org-*-cli` 技能与场景节点修正
- 组织知识库完整管理（仅透传 context 摘要）
- 前端页面、跨组织下派、子 Issue 内二次任务拆分

## 契约

| 场景 | 返回 |
|------|------|
| 目标未关联组织 | 400 `INVALID_TARGET` |
| 场景解析失败 | target `failed` + `NO_TARGET_SCENE` |
| 非 owner/admin | 403 `PERMISSION_DENIED` |
| 组织不存在 | 404 `ORG_NOT_FOUND` |
| 目标或 dispatch 不存在 | 404 `ORG_REPO_NOT_FOUND` |
| 同 targets 运行中重复下派 | 409 `DISPATCH_IN_PROGRESS` |
| 同 targets 已终态重复下派 | 200 复用既有 dispatch 与 child issue |
| bot token 缺失 | 该 target `failed` + `BOT_TOKEN_MISSING`，其他目标继续 |

## 验收标准

1. 2 个已关联目标各建 1 个子 Issue，标题 `[Org #<org_issue>] ...`，target=running。
2. 重复同 targets 下派复用既有子 Issue，不重复建单。
3. 非关联 target → 400；非 owner/admin → 403。
4. 单目标 token 缺失/场景失败不阻断其他目标，汇总 `partial_failed`。
5. 子任务完成/取消 → target 与 dispatch 汇总正确（含 sweeper）。
6. cancel → 子任务取消、dispatch=cancelled；retry 复用同一 child_issue_number。
7. 6 类审计事件落 `audit_logs`；`tests/mgmt/test_org_dispatch.py` 全绿，既有测试无回归。

## 已定默认值

- 场景第 2 档数据源：`get_repo_scene(repo_key)`。
- 超时起算点：`target.started_at`；sweeper 周期 60s。
- partial_failed 后 retry 仅针对 `status=failed` 的 target。