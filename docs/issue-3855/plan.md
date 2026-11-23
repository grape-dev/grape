# Plan: Issue #3855

## 关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 重名约束层级 | 业务层校验（Python casefold） | 存量不清洗、跨 scope 允许；不动 UNIQUE(skill_name, scope, owner_id) |
| 重名比较 | 去首尾空格 + casefold，Python 侧比较 | 规避 MySQL/SQLite LOWER/TRIM 语义差异，含 Unicode |
| 长度口径 | 去首尾空格后 `len()`（Unicode 码点） | 汉字/字母/数字/空格/emoji 各计 1 |
| 官方技能编辑 | 仅放开 display_name，其余字段 403 | 附件附录默认口径 |
| 重新上传语义 | `upsert_skill_obs(display_name=None)` 保留非空名称，空回填源名称 | 不覆盖用户命名，兼容既有调用方 |
| 上传必填 | Form 显式传入时强校验；字段缺省按历史调用回退源名称 | 前端必传，同时不破坏既有上传测试/客户端 |
| 错误码 | 必填/超长 400；重名 409 | 与既有同名冲突（409）一致 |

## 改动点

- `skills.py`：新增 `SKILL_DISPLAY_NAME_MAX`、`_normalize_skill_display_name`、
  `_skill_display_name_conflict`、`_validate_skill_display_name`；upload/patch/update 接入。
- `runtime/public`：新增 `skill_display_names_by_scope`；`skill_meta_patch_update` 补列；
  `skill_id_by_owner_scope` 补回 `display_name` 供保留判断。
- `mgmt/user/db.py`：`upsert_skill_obs(display_name=...)` 保留/回填语义。
- `SkillsView.vue` + locales：名称字段、计数器、官方编辑入口、保存刷新、文案。
- `tests/mgmt/test_issue3855_skill_display_name.py`：20 个用例覆盖验收项。

## 测试

- 新增：`python3 -m pytest tests/mgmt/test_issue3855_skill_display_name.py`
- 回归：`tests/mgmt/test_issue3202_skill_card_meta.py`、`tests/runtime/test_issue561_skill_update.py`、
  `tests/api/test_skill_write_primitive.py`、`tests/mgmt/test_skill_upload_session.py` 等。