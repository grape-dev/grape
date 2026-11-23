# Spec: Issue #3855 技能名称优化（display_name 独立化）

## 目标

技能显示名称（`display_name`）独立于 SKILL.md frontmatter 的源名称，支持新增/编辑必填、
同 scope 去重、详情页编辑即时生效、重新上传/同步保留；展示口径统一为
「display_name 非空优先，否则回退源名称」。

## 范围

- 后端 `src/domains/mgmt/routes/skills.py`
  - `POST /api/skills/upload`：新增 `display_name` 表单字段；空/仅空格/超长 400，
    同 scope 重名 409「名称已存在」；缺省（历史 API 调用）回退源名称。
  - `PATCH /api/skills/{skill_id}`：白名单新增 `display_name`；官方技能仅放开名称，
    其余元信息维持 403；官方改名限定平台管理员（`admin_accounts`），普通 JWT/API Key 403。
  - `POST /api/skills/by-id/{id}/update`：重新上传不再覆盖已录入名称。
- 写链 `src/domains/mgmt/user/db.py`：`upsert_skill_obs` 新增 `display_name` 参数，
  显式值优先；缺省保留行内非空名称，为空回填源名称。
- 读链 `src/domains/runtime/public/__init__.py`：同 scope 名称取数查询（官方档
  scope-only，兼容 owner_id `''`/`0` 历史不一致）、`skill_meta_patch_update` 扩列
  `display_name`；详情返回 `can_edit_display_name` 供前端同步鉴权。
- 官方同步 `src/domains/runtime/storage/skill_sync.py`：30 分钟同步保留已录入的
  非空 `display_name`，仅空值回填源名称。
- 前端 `SkillsView.vue`：上传弹窗名称必填 + 计数器（上限常量单点定义）；编辑弹窗
  名称输入；官方编辑入口仅对管理员展示（`can_edit_display_name`）；保存后刷新
  卡片/详情；i18n 文案。
- 不改 `src/api/skill_display.py` 回退链语义。

## 验收

1. 展示/编辑/重名三档 scope 统一；官方技能仅管理员可改名，其余元信息仍 403。
2. 名称必填、≤20 Unicode 码点（汉字/emoji 计数 1、去首尾空格）。
3. 同 scope 重名（空格/大小写变体）阻断并提示「名称已存在」；跨 scope 同名允许。
   去重仅约束 display_name；空名存量行的源名称兜底展示按附件 §7.1/§8.2 不纳入（同 scope 展示重名属可接受口径）。
4. 重新上传/同步保留已录入名称；存量空名回退源名称、不清洗。
5. 既有测试通过 + 新增必填/边界/Unicode/重名/官方鉴权/官方同步保留/存量回退覆盖。