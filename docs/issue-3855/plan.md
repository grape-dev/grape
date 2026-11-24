# Plan: Issue #3855

## 关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 重名约束层级 | 业务层校验（Python casefold） | 存量不清洗、跨 scope 允许；不动 UNIQUE(skill_name, scope, owner_id) |
| 重名比较 | 去首尾空格 + casefold，Python 侧比较；内部空格不折叠 | 规避 MySQL/SQLite LOWER/TRIM 语义差异，含 Unicode；与附件口径一致 |
| 重名范围 | 仅比较 `display_name` 列；空名存量行的源名称兜底展示不参与 | 附件 §7.1 明确源名称不纳入重名约束、§8.2 接受存量源名重复；同 scope 展示重名属既定口径 |
| 转换名称 | 显式计算有效展示名：源自定义名优先，超长截断到 20，同 scope 重名追加 `~N` | 避免 `display_name=None` 回填超长源名称绕过长度上限 |
| 官方重名取数 | scope-only（忽略 owner_id） | 官方行 owner_id 历史并存 `''` 与 `0` |
| 长度口径 | 去首尾空格后 `len()`（Unicode 码点） | 汉字/字母/数字/空格/emoji 各计 1 |
| 官方技能编辑 | 仅放开 display_name，且限平台管理员；管理台 JWT 不得改/读非 official 技能；其余字段 403 | 附件附录默认口径 + CONTRIBUTING §7「不弱化鉴权」；admin/grape 两套 id 独立，防撞号越权 |
| 重新上传语义 | `upsert_skill_obs(display_name=None)` 保留非空名称，空回填源名称 | 不覆盖用户命名，兼容既有调用方 |
| 上传必填 | Form 显式传入时强校验；字段缺省按历史调用回退源名称 | 前端必传，同时不破坏既有上传测试/客户端 |
| 错误码 | 必填/超长 400；重名 409 | 与既有同名冲突（409）一致 |

## 改动点

- `skills.py`：新增 `SKILL_DISPLAY_NAME_MAX`、`_normalize_skill_display_name`、
  `_skill_display_name_conflict`、`_validate_skill_display_name`、
  `_is_official_skill_admin`；upload/patch/update 接入。
- `runtime/public`：新增 `skill_display_names_by_scope` / `skill_display_names_by_official_scope`；
  `skill_meta_patch_update` 补列；`skill_id_by_owner_scope` 补回 `display_name` 供保留判断。
- `runtime/storage/skill_sync.py`：官方同步保留非空 display_name，空值回填源名称。
- `mgmt/plugins/registry.py`：官方技能 owner_id 统一写 `''`。
- `mgmt/user/db.py`：`upsert_skill_obs(display_name=...)` 保留/回填语义。
- `SkillsView.vue` + locales：名称字段、计数器（上限常量）、管理员专属官方编辑入口、保存刷新、文案。
- `tests/mgmt/test_issue3855_skill_display_name.py`：25 个用例覆盖验收项（含官方鉴权与同步保留）。

## 测试

- 新增：`python3 -m pytest tests/mgmt/test_issue3855_skill_display_name.py`
- 回归：`tests/mgmt/test_issue3202_skill_card_meta.py`、`tests/runtime/test_issue561_skill_update.py`、
  `tests/api/test_skill_write_primitive.py`、`tests/mgmt/test_skill_upload_session.py` 等。