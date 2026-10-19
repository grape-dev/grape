# Spec: Issue #248 — repo_key 规范化修复

## Objective
修复仓库引擎和设备注册时 repo_key 未规范化的问题：`create_runtime_credential` 和 `change_credential_scope` 直接存入前端传入的 repo_key（可能无 provider 前缀或为中文名），导致可见性过滤时与登录态仓库英文名称不匹配。

## Root Cause
1. `create_runtime_credential` (settings.py:838-848)：`body.repo_key` 仅 `.strip()` 后直接入库，未调用 `_resolve_repo_key` 规范化
2. `change_credential_scope` (settings.py:909-924)：同上
3. `_make_visible_credential_fn` (settings.py:543)：`cowner_id in user_repos` 比对格式不一致 — `cowner_id` 可能带 `gitcode:` 前缀，`user_repos` 无前缀（`from_repo_key` 已剥前缀）

## Fix
| 位置 | 改动 |
|------|------|
| `create_runtime_credential` | 存储前调用 `_resolve_repo_key(raw_key)` 规范化 |
| `change_credential_scope` | 同上 |
| `_make_visible_credential_fn` | repo scope 比对用 `from_repo_key(cowner_id)` 剥前缀后与 `user_repos` 比对 |
| `_build_cred_map` | SELECT 增加 `repo_key` 列，供前端展示 |
| `get_agent_runtimes` / `_build_runtime_devices` | 响应增加 `repo_key` 字段（已剥前缀的 owner/repo） |
| `list_runtime_credentials` | 响应增加 `repo_key` 字段 |
| `runtimes.js` `_renderAddDeviceCredForm` | scope=repo 时自动填充 `app.currentRepo().fullName` |
| `runtimes.js` `openScopeSwitcher` | scope=repo 时自动填充当前仓库 |
| `runtimes.js` `renderWorkers` | scope=repo 的 worker 卡片显示仓库标识 |

## Acceptance Criteria
- [x] 注册时 repo_key 经 `_resolve_repo_key` 规范化（带 provider 前缀存储）
- [x] scope 切换时同上
- [x] 可见性过滤用 `from_repo_key` 统一剥前缀比对
- [x] scope=user/global 凭据不受影响（回归验证）
- [x] 前端 scope=repo 时自动填充当前登录仓库
- [x] 新增测试覆盖规范化场景

## Testing Strategy
- pytest（tests/test_runtime_protocol.py）：新增 `TestRepoKeyNormalization` 类
- 断言存储值带前缀、可见性匹配正确、user/global 回归不受影响
