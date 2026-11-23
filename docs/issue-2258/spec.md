# Issue #2258 需求文档：手动绑定已有仓库为组织仓库（provisioning 兜底）

## 目标
自动 provisioning 失败（owner 无 VCS token、建仓失败等）时，允许组织 owner/admin 手动把
已托管仓库指定为组织仓库并补齐组织场景，作为兜底通道，让组织尽快恢复可用。

## 范围
### In Scope
- 新增 `PUT /api/orgs/{id}/provision/repo`，body `{repo_key, confirm?}`。
- owner/admin 鉴权；组织不存在 404 `ORG_NOT_FOUND`。
- 仓库校验：未托管 404 `REPO_NOT_HOSTED`；属其他组织 409 `REPO_ALREADY_BOUND`；
  `org_id=0` 视为可绑定。
- 写入 `organization.org_repo_key`；`org_scene_id` 为空时用 `create_org_scene` 建默认
  3 节点场景；置 `provision_status=ready`、清空 `provision_error`。
- 替换保护：已有 `org_repo_key` 且不同，需 `confirm=true` 且无运行中 dispatch 引用旧仓库，
  否则 409（`CONFIRM_REQUIRED` / `ORG_REPO_EXISTS`）。
- 审计事件 `org_provision_manual_bind`。
- 新增 `tests/mgmt/test_org_manual_bind.py`。

### Out of Scope
- 不自动新建仓库；不做组织仓库迁移；不做前端；不改表结构。

## 验收标准
- 400 `INVALID_REPO` / `CONFIRM_REQUIRED`；403 `PERMISSION_DENIED`；
  404 `ORG_NOT_FOUND` / `REPO_NOT_HOSTED`；409 `REPO_ALREADY_BOUND` / `ORG_REPO_EXISTS`。
- 成功绑定后 `org_repo_key` / `org_scene_id` / `provision_status` 正确且 `provision_error` 清空。
- 审计落库；自动 provisioning 不回归。