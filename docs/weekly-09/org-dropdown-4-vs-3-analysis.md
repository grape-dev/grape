# 仓库设置"目标组织"4项 vs 右上角"我的组织"3项 — 差异分析

日期: 2026-10-31
影响用户: songspace（grape_accounts.id=3, 吕军涛）

## 现象

- 仓库设置 → 迁移仓库 →「目标组织」下拉框：4 个选项
- 右上角「我的组织」下拉框：3 个选项

## 实测数据（本机 master 真实 API 返回）

`GET /api/orgs`（右上角"我的组织"数据源）返回 3 个组织：

| id | 名称 | slug | 类型 |
|----|------|------|------|
| 102 | songspace的个人空间 | personal-3 | 个人空间 |
| 191 | 社区博客团队 | cskills-team | 团队 |
| 206 | AI洞察分析团队 | ai洞察分析团队 | 团队 |

`GET /api/repos/migrate-orgs`（仓库设置迁移下拉数据源）返回 3 个组织 **+ 1 个 personal 字段（`"personal": "songspace"`）**。

前端 `settings.js _loadMigrateOrgs()`（settings.js:117-145）渲染逻辑：

1. 先追加一个独立选项：`songspace (个人)`（来自 `d.personal`，即用户登录名）
2. 再逐个追加 `d.orgs` 里的组织，**其中个人空间组织也照常输出**，login 取 slug → 显示为 `personal-3 (songspace的个人空间)`

所以下拉框里实际是这 4 项：

```
songspace (个人)                      ← personal 字段额外生成的"迁到个人名下"选项
personal-3 (songspace的个人空间)      ← 个人空间组织又出现一次（slug 作为 login）
cskills-team (社区博客团队)
ai洞察分析团队 (AI洞察分析团队)
```

## 根因（一句话）

不是数据丢了——两个下拉数据口径不同：右上角列的是「组织」（个人空间算 1 个组织，共 3 个）；仓库设置迁移下拉在 3 个组织之外又额外加了「songspace（个人）」这个"迁到个人名下"选项，而**个人空间组织没有被去掉**，导致"个人"出现了两次（一次叫 `songspace (个人)`，一次叫 `personal-3`），所以是 4 项。

涉及代码：

- 后端 `src/api/routes/repos.py:2030 list_migrate_orgs`：返回 `{orgs: [...含 is_personal 组织], personal: username}`，个人空间重复计入。
- 前端 `src/api/static/js/pages/settings.js:117 _loadMigrateOrgs`：无条件渲染 personal 选项 + 全量 orgs 选项，无去重。
- 右上角 `src/api/static/js/app.js:355 _loadTbOrgs`：只渲染 `/api/orgs`（3 项），无 personal 附加项——两边口径天然不一致。

## 修复方案

### 推荐：后端去重（治本，改动 1 处）

`list_migrate_orgs`（repos.py:2056）组包时把 `is_personal` 的组织从 `orgs` 列表中过滤掉——个人迁移目标已由 `personal` 字段单独表达，语义唯一：

```python
# repos.py list_migrate_orgs 组包处
for r in rows:
    if r[3]:          # is_personal=True 的组织跳过，personal 字段已覆盖
        continue
    orgs.append({...})
```

效果：迁移下拉变为 3 项 = `songspace (个人)` + 2 个团队组织，与右上角数量一致，且"个人"只出现一次、名称统一显示登录名。

兼容性检查：

- `/repos/migrate` 校验 3（repos.py:2117，按 slug 查目标组织）不受影响——迁到个人走 `target_owner="songspace"`，本来就不匹配 personal-3 这个 slug；过滤后反而消除了用户误选 `personal-3`（一个丑陋 slug、且语义上仍指个人空间）的歧义路径。
- 前端 `_loadMigrateOrgs` 无需改动（orgs 少一项自动少渲染一行）；`personal` 选项与 `curOwner` 的排除逻辑（settings.js:126/132）保持不变。
- 其它调用方：`/repos/migrate-orgs` 仅被 settings.js 消费（全仓 grep 确认），无第二消费点。

### 备选：前端过滤（不动后端）

settings.js 循环里跳过 `o.is_personal`：

```javascript
for (var i = 0; i < orgs.length; i++) {
  var o = orgs[i];
  if (o.is_personal) continue;   // personal 选项已单独渲染
  ...
}
```

效果相同，但后端仍返回冗余数据，治标。

### 双主发布注意

改动需同步 m2（另一台 master）并各自生效；前端 JS 属静态文件，用户强刷缓存即可，后端 py 改动需重启 uvicorn（**须用户批准后执行**）。

## 验证步骤

1. 部署后用 songspace 的 JWT 调 `GET /api/repos/migrate-orgs`：确认 `orgs` 不含 `is_personal=true` 条目，`personal:"songspace"` 仍在。
2. 前端打开仓库设置迁移弹窗：下拉应显示 3 项（`songspace (个人)` + 2 团队），与右上角"我的组织"数量一致。
3. 实际执行一次迁移到团队组织，确认 403/成功路径行为不变。
