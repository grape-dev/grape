# Spec: Issue #2090 组织仓库与组织流水线自动创建

## Objective

团队组织创建后自动获得私有组织 VCS 仓库与默认组织场景，组织管理员可关联、解绑、迁移已托管仓库。个人组织不触发 provisioning，所有 provisioning 在后台执行，失败不阻塞组织创建并且可重试。

## Commands

```bash
python3 -m pytest tests/mgmt/test_org_provisioning.py tests/mgmt/test_org_repo_binding.py -q
python3 -m pytest tests/mgmt -q
```

## Project Structure

- `src/api/db_setup.py`: 幂等数据库列迁移。
- `src/api/pipeline_config.py`: 组织默认场景定义。
- `src/domains/mgmt/user/org_db.py`: 组织 provisioning 字段与组织仓库数据操作。
- `src/domains/mgmt/org_provision.py`: 组织仓库、托管、场景的状态机编排。
- `src/domains/mgmt/routes/orgs.py`: 创建组织触发 provisioning，查询与重试端点。
- `src/domains/mgmt/routes/org_repos.py`: 组织仓库关联、解绑、迁移端点。
- `tests/mgmt/test_org_provisioning.py`: provisioning 测试。
- `tests/mgmt/test_org_repo_binding.py`: 组织仓库归属测试。

## API Contract

- `POST /api/orgs`: 团队组织创建成功后返回 `provision_status=pending`，后台推进 provisioning。
- `GET /api/orgs/{id}/provision`: 返回状态、仓库键、场景 ID、错误与完成时间。
- `POST /api/orgs/{id}/provision/retry`: 组织管理员重试失败/未完成的 provisioning。
- `GET /api/orgs/{id}/repos`: 列出组织关联仓库。
- `POST /api/orgs/{id}/repos`: 关联已托管仓库，同一 `repo_key` 单归属；同组织重复关联幂等返回现有绑定，个人组织归属按未归属处理。
- `DELETE /api/orgs/{id}/repos/{repo_key:path}`: 二次确认后解绑普通关联仓库；与任务入队共用仓库级分布式租约，复查存在 queued/dispatched/running/pending_self_report 派发任务时拒绝。
- `POST /api/orgs/{id}/repos/{repo_key:path}/transfer`: 将普通关联仓库迁移到目标组织；配额判定与写入在目标组织绑定租约内完成。
- `POST /api/repos/migrate`: 复用组织间 transfer DAL 改写仓库组织归属，在目标组织绑定租约内完成保护、`max_repos` 校验和审计。

错误码：

- `400 INVALID_ORG_CONTEXT`: 个人组织或非法目标组织上下文。
- `400 INVALID_REPO`: 非法仓库键。
- `403 PERMISSION_DENIED`: 非组织管理员或无 VCS maintainer/admin 权限。
- `404 ORG_NOT_FOUND`: 组织不存在。
- `404 REPO_NOT_HOSTED`: 仓库未托管或不属于路径组织。
- `409 REPO_ALREADY_BOUND`: 仓库已归属其它组织。
- `409 ORG_REPO_DISPATCH_RUNNING`: 仓库仍有运行中的派发任务，暂不能解绑。
- `409 ORG_REPO_BUSY`: 并发绑定、解绑或派发租约正在处理该仓库，客户端稍后重试。
- `409 ORG_REPO_PROTECTED`: 自动 provisioning 的组织仓库禁止解绑或迁移。
- `409 ORG_REPO_QUOTA_EXCEEDED`: 目标组织超过 `max_repos`。

配额口径：`max_repos` 统计目标组织 `managed_repos.org_id` 下的全部托管仓库，包含自动 provisioning 的组织仓库；个人组织归属不占团队组织配额。

## Testing Strategy

使用 pytest、FastAPI TestClient、真实 SQLite 测试库和依赖注入 mock 身份。VCS 创建与托管调用通过 monkeypatch 隔离，断言数据库状态、HTTP 错误契约、幂等与配额行为。

## Boundaries

- Always: provisioning 只通过 `org_id` 幂等推进，写审计，保持个人组织与非组织仓库行为不变。
- Ask first: 不新增第三方依赖，不改 VCS 平台协议。
- Never: 在日志或数据库中写入 token，不执行 `org-*-cli` 或组织编排逻辑。

## Success Criteria

- 团队组织从 `pending` 到 `ready`，失败进入 `failed` 并可重试。
- 重复执行 provisioning 不重复建仓、托管或建场景；自动仓库托管后统一通过目标组织绑定 DAL 校验配额并落组织归属；建仓成功但未落库时仅当仓库为私有且描述匹配 provisioning 归属证据才按确定性 full_name 认领。
- provisioning 租约丢失时，本尝试的中间态 CAS 置为 `failed`（`ORG_PROVISION_LEASE_LOST`），不覆盖新持有者的推进。
- 个人组织不触发 provisioning。
- 组织仓库 `is_org_repo=1` 时解绑/迁移返回 `ORG_REPO_PROTECTED`。
- 单仓库单组织归属，冲突返回 `REPO_ALREADY_BOUND`。
- 关联、迁移（含 `/api/repos/migrate`）占用 `max_repos` 配额并写审计；配额判定与写入共用目标组织绑定租约，并发不可突破。
- `agent_tasks(repo_key, status)` 有幂等索引支撑派发拦截查询。

## Open Questions

无。
