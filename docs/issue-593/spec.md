# Spec — Issue #593 仓库设置迁移目标组织下拉框去重

> Coding Agent | 基于 Issue Agent 发布的 Spec 实施 | 2026-10-31

## 目标
后端 `/api/repos/migrate-orgs` 接口去重：`list_migrate_orgs` 组包 `orgs` 列表时跳过 `is_personal=True` 的组织条目（个人组织已由 `personal` 字段独立返回），消除迁移目标组织下拉框中重复的个人项。

## 范围
- In：`src/api/routes/repos.py` 的 `list_migrate_orgs` 函数（repos.py:2056 组包处）增加 `is_personal` 过滤。
- Out：前端 `settings.js` 不改；`/repos/migrate` 校验逻辑不改（按 slug 查目标组织，不受 orgs 列表去重影响）。

## 方案
在 `list_migrate_orgs` 的 `for r in rows:` 循环里增加 `if r[3]: continue`（r[3] = is_personal 列），跳过个人组织条目。`personal` 字段照常返回 `user["username"]`。

## 改动文件
| 模块 | 类型 | 说明 |
|------|------|------|
| `src/api/routes/repos.py` | 修改 | `list_migrate_orgs` 组包过滤 `is_personal`（1 处） |
| `tests/test_repo_migrate_orgs.py` | 修改 | 旧 `test_migrate_orgs_personal_org_included` 与新需求冲突，改为 `test_migrate_orgs_personal_org_excluded_from_orgs`；新增 `test_migrate_orgs_personal_field_still_returned` 覆盖 personal 字段仍在 |

## 验收
1. `GET /api/repos/migrate-orgs`：`orgs` 不含 `is_personal=true` 条目，`personal` 字段仍在。
2. 所有现有测试通过 + 新增/修改测试覆盖 is_personal 过滤逻辑。

## 发布注意（双主）
改动需同步 m2 另一台 master；后端 py 改动需重启 uvicorn（须用户批准后执行）。
