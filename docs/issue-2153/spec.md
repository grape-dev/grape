# Spec: Issue #2153 — 组织流水线端到端集成用例

> 权威 Spec 见 Issue #2153 评论（Issue Agent 发布，id 200291646）。本文件是 Coding 阶段
> 落地的需求摘要，用于 PR 内自解释；口径与 Issue Spec 一致。

## Objective

新增 `tests/integration/test_org_pipeline_e2e.py`，以可重复、无外网的集成测试锁定组织主体链路：
组织 provisioning → 关联 2 仓库 → 组织需求 → 下派 2 个目标仓库 → 子任务完成/失败 → 聚合。

被测能力（`provisioning` / 关联仓库 / `dispatch` / 聚合）已随 #2090、#2112、#2118 合入，本单只补回归保护，不新建能力。

## Scope

### In

- `tests/integration/conftest.py`：每用例独立 `tmp_path` SQLite + `TestClient` + 固定身份/token + 仅 VCS adapter mock。
- `tests/integration/test_org_pipeline_e2e.py`：happy path（含 partial_failed）单用例顺序推进 + 幂等用例 + cancel 用例。
- `pytest.ini`：注册 `integration` marker；默认回归不纳入（独立命令执行）。
- `.github/workflows/windows-smoke.yml`：新增 `integration` 测试门禁步骤。

### Out

- 组织上下文注入（#2150 未合入，用例不 import 组织上下文模块）。
- 真实 VCS 建仓/托管、真实 worker、前端。
- 暴露缺陷的修复（只记录 + 另开 Issue）。

## 关键决策

| 决策 | 选择 |
|------|------|
| mock 边界 | 仅 VCS 建仓/托管/bot token（adapter 层）+ worker 派发入口；保留真实 DB、真实 dispatch 状态机 |
| 子任务状态推进 | 真实状态迁移：完成走 issue 完成路径；失败走 sweeper 真实超时路径；不直改 `dispatch` status、不直调聚合 |
| 幂等判据 | targets 集合比对 + 子 Issue 数量不变 + batch 复用 |
| 隔离 | 每用例独立 `tmp_path` SQLite 文件（重绑 `session_manager.db` 并在 teardown 还原） |
| 确定性 | 固定用户 id/username/bot token；失败用 timeout 常量收缩而非真实等待 |

## Acceptance

- [ ] 组织需求端到端到 2 个目标仓库，子 Issue 与 target 一一对应。
- [ ] completed / partial_failed / cancelled / 幂等四类断言正确（落在 `dispatch` 状态与子 Issue 数量）。
- [ ] 不依赖外网与真实 worker；`pytest -m integration` 全绿；现有套件不受影响。
- [ ] 暴露真实缺陷时附最小复现另开 Issue，不夹带修复。
