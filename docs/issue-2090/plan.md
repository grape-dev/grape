# Implementation Plan: Issue #2090

## Overview

在 mgmt 域增加组织 provisioning 状态机与组织仓库绑定 API。数据库先完成幂等加列，服务层按“建仓 -> 托管 -> 建场景”补缺推进，路由层只负责鉴权、错误契约和异步触发。

## Architecture Decisions

- 使用 `organization.provision_status` 持久化状态，并通过 `distributed_locks` 的 per-org 租约防止多实例并发重复执行。
- 复用 `create_new_repo`、`host_repo` 和 `create_custom_scene`，不重写 VCS 与场景逻辑。
- 组织自动仓库写 `is_org_repo=1`，普通关联仓库写 `is_org_repo=0`。
- `managed_repos.repo_key` 是单归属唯一键，绑定、解绑、迁移只改组织关联字段。

## Task List

### Phase 1: Contract and Foundation

- [x] Task 1: 增加 organization/managed_repos 迁移与组织 DAL 字段操作。
- [x] Task 2: 增加组织默认场景模板与幂等 creation helper。

### Checkpoint: Foundation

- [x] 新列在现有测试库幂等创建，旧组织和仓库行为不变。

### Phase 2: Provisioning

- [x] Task 3: 实现 `org_provision.py` 状态机、补缺逻辑、失败记录和重试。
- [x] Task 4: 组织创建触发、provision 查询与 retry 路由。

### Checkpoint: Provisioning

- [x] mock VCS 下团队组织到 `ready`，失败可重试且不重复建资产。

### Phase 3: Organization Repo Binding

- [x] Task 5: 实现组织仓库列表、关联、解绑、迁移、配额、保护与审计。
- [x] Task 6: 注册路由并补充契约测试。

### Checkpoint: Complete

- [x] 新增测试与 mgmt 回归测试通过。

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 并发 retry 重复建仓 | High | distributed_locks per-org 租约；建仓失败时按确定性 full_name 认领已存在仓库 |
| 组织绑定入口绕过保护/配额 | High | bind/transfer/unbind/migrate/provisioning 统一复用目标组织绑定 DAL 与租约 |
| 解绑与派发入队跨实例竞态 | High | 解绑和 queued/direct 任务入队共享仓库级分布式租约 |
| 旧库缺列 | High | 使用 `_ensure_column` 幂等迁移并兼容旧表 |
| 迁移仓库关联数据不一致 | Medium | 在单事务中更新 managed_repos 及核心关联表 |
| 外部 VCS 失败阻塞组织创建 | High | 后台线程执行，异常只写 `failed + provision_error` |

## Open Questions

无。
