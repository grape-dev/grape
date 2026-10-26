# Issue #448 需求文档：子技能端点支持 OBS 存储的 user/repo 技能（references/ zip 提取）

- Issue: https://gitcode.com/grape-dev/grape/issues/448
- 分支：`fix/issue-448-subskill-obs-extract`（基线 `origin/weekly-08` @ 5456fb041）
- 状态：已实现（待评审合并）
- 类型：bug 修复（#444 全链路扫描 backlog F2）

## 1. 背景与问题

子技能三端点只读本地 `SKILLS_DIR` 磁盘目录：

- `GET /api/skills/{skill_id}/sub`（列表）
- `GET /api/skills/{skill_id}/sub/{sub_name}`（详情）
- `GET /api/skills/package-sub/{pkg_id}/{sub_name}`（包详情，同 detail 形状）

user/repo 技能自 v2.4 起存 OBS 对象（`file_path` 为对象键，本地无目录）→
列表返回**空列表假象**（200 + `sub_skills: []`，Issue 原文写"一律 404"仅对两个详情端点成立），
详情 404。来源：Issue #444 backlog F2（`issue-444-skill-cache-sync-requirements.md` §4.4 / UC6-3）。

## 2. 需求项

| 编号 | 需求 | 来源 |
|---|---|---|
| R32 | 三端点在本地 references/ 未命中时从 OBS zip 按 entry 提取子技能（列表含 frontmatter 解析与文件计数；详情含二级 references 顶层附件，各截 2000 字符） | Issue 验收 1 |
| R33 | official 技能本地目录优先，行为零改动（有对象键也不走 zip，覆盖"官方技能转集群"场景的顺序保证） | Issue 验收 2 |
| R34 | 复用既有两级缓存：zip 磁盘缓存命中不回源 OBS（storage.read_bytes 计数不增） | Issue 验收 3 |
| R35 | 有对象键但存储未启用 → 503（对齐 detail/convert 端点口径）；无对象键（历史数据未迁移）→ 沿用既有 404/空列表口径，不引入新失败面 | Spec 口径 |

### 不在范围

- 不改 official 技能同步/双机目录逻辑；不改 zip 上传规范化（`_normalize_skill_zip`）。
- 不做子技能独立内存缓存（复用 `zip_cache`，失效链路 `invalidate_skill_caches` 不扩）。
- 不修前端页面；不修 locales error_code 键嵌套问题（已知独立 bug，另单）。
- 不做 `references/{sub}` 目录名与 skill_name 不一致时的兜底（对齐 `extract_skill_md` 常规技能精确匹配语义）。

## 3. 场景与场景用例（5 个场景，UC↔测试映射见 §6）

- S1 核心修复（user 技能 OBS zip 提取）：UC1 列表 / UC2 详情 / UC3 package-sub
- S2 兼容/回归（official 行为不变）：UC4 本地优先（zip 为垃圾字节也不读 OBS）
- S3 性能口径（缓存不回源）：UC5 二次读取 read_bytes 计数 == 1
- S4 降级边界：UC6 storage=None + 有对象键 → 503；UC7 历史无对象键 → 空列表
- S5 安全守卫：UC8 sub_name 路径穿越 → 404（entry 精确匹配天然免疫）；UC9 zip 内无此子技能 → 404

## 4. 实现要点

- `src/storage/__init__.py` 新增 `list_zip_sub_skills(zip_bytes, skill_name)` 与
  `read_zip_sub_skill(zip_bytes, skill_name, sub_name)`：entry 精确匹配
  `{skill_name}/references/{sub}/SKILL.md`，namelist 全量枚举后比对，免疫 `../` 穿越；
  附件仅读 `{sub}/references/` 顶层文件（对齐本地 `os.listdir` 行为）。
- `src/api/routes/skills.py` 新增 `_get_sub_skill_zip(skill_id, skill_name)` 共用辅助：
  `get_skill_object_key` → 无键返回 None（沿用旧口径）；有键且 storage=None → 503；
  否则 `get_skill_zip_bytes`（磁盘缓存 → OBS 回源）。三端点在本地目录/文件未命中处插入该分支。
- 顺序保证：本地（`resolve_official_skill_path` / SKILLS_DIR 拼接）命中优先，OBS 仅作 fallback——
  official 技能（含转集群后 file_path 有对象键的）行为不变。

## 5. 实测核验

- `tests/test_issue_448_sub_skill_obs.py` 9 用例全绿（FakeStorage 内存模拟 + 共享测试 SQLite，
  DB 自建行自清理，zip 磁盘缓存按对象键失效隔离）。
- 家族回归：`test_skills_obs / test_skill_local_storage / test_skill_sync_obs /
  test_issue_406_skill_display_name / test_scene_skill_market` 共 31 failed / 85 passed，
  与基线（git stash 后重跑）**完全一致**——既有失败（含 `test_sub_skill_path.py` 4 例，
  其断言的 `skills/sre/huawei-cloud` 已迁移至 `skills/sre/app-release/huawei-cloud`，路径过期），零新增回归。

## 6. UC↔测试映射

| UC | 测试函数 | 状态 |
|---|---|---|
| UC1 | `test_list_sub_skills_from_obs` | ✅ |
| UC2 | `test_read_sub_skill_from_obs` | ✅ |
| UC3 | `test_package_sub_from_obs` | ✅ |
| UC4 | `test_official_local_dir_priority` | ✅ |
| UC5 | `test_zip_cache_hit_no_refetch` | ✅ |
| UC6 | `test_storage_disabled_503` | ✅ |
| UC7 | `test_legacy_user_skill_without_object_key_empty` | ✅ |
| UC8 | `test_sub_name_traversal_404` | ✅ |
| UC9 | `test_sub_not_in_zip_404` | ✅ |

部署类验收（真实 OBS / 双进程）非单测材料，按 Issue 验收清单在目标环境执行。

Refs #448
