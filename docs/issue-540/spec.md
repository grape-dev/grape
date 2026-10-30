# Spec — Issue #540: 支持仓库从 A 组织迁移到 B 组织

## 目标

在托管仓库设置中提供「迁移到其他组织」能力，原子改写 `repo_key` 及全部关联数据，
无需「解除托管 → 重新托管」两步操作。GitCode 侧仓库转移（owner 变更）由用户在
GitCode 完成，Grape 承接「换绑」：把平台内全部以旧 `repo_key` 为索引的记录原子改写
为新 `repo_key`，webhook/bot token 沿用原配置不重建，工作区目录物理迁移。

## 范围

- 新增 `POST /api/repos/migrate` 端点（`src/api/routes/repos.py`）
- 新增原子改写模块 `src/repo_migrate.py`（复用 `_migrate_repo_key_tables` 的表清单与
  UNIQUE 冲突处理思路，镜像 `src/repo_cleanup.py` 结构）
- 工作区目录物理迁移：`workspace_repo_dir(old)` → `workspace_repo_dir(new)`
- 前端迁移入口（`settings.js` + i18n）
- 单测覆盖：批量改写、UNIQUE 冲突、失败回滚、工作区目录迁移

## 不在范围

- 不调用 GitCode 仓库转移 API（GitCode 侧 owner 变更由用户在 GitCode 完成）
- 不重建 webhook、不重新邀请 bot（webhook/bot 随仓库转移自动跟随，仅改 repo_key 映射）
- 不做跨 provider 迁移（gitcode→gitcode / github→github）

## API

`POST /api/repos/migrate`
```json
{ "repo_key": "gitcode:ownerA/repo", "target_owner": "ownerB", "target_repo": "repo" }
```
- `repo_key`：源仓库（当前托管记录的 repo_key，带 provider 前缀）
- `target_owner`/`target_repo`：目标组织/仓库名（GitCode 侧已转移后的新地址）

## 前置校验（失败返回明确错误，不破坏现有托管）

1. 源 repo_key 存在于 managed_repos（否则 404）
2. 目标 repo_key 不存在于 managed_repos（否则 409 冲突）
3. 源与目标 provider 一致（禁止跨 provider，否则 400）
4. 目标仓库在 GitCode 侧存在且当前用户有 maintainer 及以上权限（复用
   `get_member_permission` / `get_github_collaborator_permission`，确认转移完成 + 权限）
5. 源仓库当前用户为 is_join/is_admin（Grape 侧归属校验）

## 原子改写（单事务，失败 rollback）

按 `repo_cleanup._REPO_KEY_TABLES` 全量表 + managed_repos + user_repos + skill + meta：
- managed_repos：UPDATE repo_key/repo_owner/repo_name/full_name
- user_repos：UPDATE repo_key/owner/name（UNIQUE(user_id,repo_key) 冲突时删旧留新）
- 其余 _REPO_KEY_TABLES：UPDATE repo_key（UNIQUE(repo_key) 冲突时删旧留新）
- skill(scope='repo')：UPDATE owner_id
- meta：onboarded:{old} → onboarded:{new}；cleanup_pending:{old} → {new}

DB 提交后迁移工作区目录：`shutil.move(workspace_repo_dir(old), workspace_repo_dir(new))`。

## 验收标准

- [ ] 迁移入口可用，前置校验失败有明确报错
- [ ] repo_key 改写为原子操作：全部关联表一起生效或一起不生效
- [ ] webhook/bot token 沿用原配置，不重建
- [ ] 场景配置与 Issue 关联保留（随 repo_key 改写跟随）
- [ ] 工作区目录迁移完成
- [ ] 迁移后新 repo_key 可见、webhook 正常触发、Issue 历史可追溯
- [ ] 失败场景明确报错且不破坏现有托管
- [ ] 单测覆盖批量改写与失败回滚路径
