# Issue #2258 方案设计

## 分层
```
routes/orgs.py                 校验组织存在 / owner-admin 鉴权 / 调用服务 / 返回 provision payload
        ↓
mgmt/org_provision.py          manual_bind_org_repo：repo_key 归一化、归属校验、替换保护、场景补齐、审计
        ↓
user/org_db.py                 managed_repos 绑定为组织仓库 / 清旧标记 / 运行中 dispatch 查询
auth/audit.py                  注册并写入 org_provision_manual_bind 审计事件
```

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| repo_key 归一化 | provider 前缀 + `owner/repo` 双格式，非法即 400 INVALID_REPO | 与 `org_repos` 绑定接口一致 |
| 归属判定 | `org_id in (0, 当前组织)` 可绑定 | 兼容已托管未归属仓库 |
| 替换确认 | 未带 confirm → 409 CONFIRM_REQUIRED | 避免误切组织仓库 |
| 运行中引用 | `org_dispatch.status in (dispatching, running, aggregating)` → 409 ORG_REPO_EXISTS | 防止替换打断在途下派 |
| 场景创建 | 仅 `org_scene_id` 为空时 `create_org_scene` | 防止覆盖既有场景与节点 |
| 旧仓库标记 | 替换成功后清除旧仓库 `is_org_repo` | 保持“单组织仓库”语义 |
| 表结构 | 复用 organization / managed_repos 现有字段 | Issue 明确不改表 |

## 错误码
| HTTP | error_code | 场景 |
|------|-----------|------|
| 400 | INVALID_REPO | repo_key 缺失/格式非法 |
| 403 | PERMISSION_DENIED | 非组织 owner/admin |
| 404 | ORG_NOT_FOUND | 组织不存在 |
| 404 | REPO_NOT_HOSTED | 仓库未托管 |
| 409 | REPO_ALREADY_BOUND | 仓库已属于其他组织 |
| 409 | CONFIRM_REQUIRED | 替换已有组织仓库但未 confirm |
| 409 | ORG_REPO_EXISTS | 旧组织仓库仍被运行中 dispatch 引用 |

## 测试 seam
HTTP seam：`PUT /api/orgs/{id}/provision/repo`（经 `app.dependency_overrides` 注入用户）。
服务 seam：`org_provision.manual_bind_org_repo`（与自动 `provision_org` 共享 `_ensure_org_scene`）。