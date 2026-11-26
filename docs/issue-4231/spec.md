# Spec: Issue #4231 — check-name 缺少名称字符/长度校验、错误前缀重复

> 来源：Issue #4231 Spec Document v3（issue agent 评论 203778699，目标分支 `arch-weekly-11-ljt`）。
> 本文为开发侧对齐稿，不重复展开需求。

## 需求（同 Issue Spec v3）
- `check-name` 对 `name` 无字符集/长度校验，非法名称（含空格/中文/超长）一路打到上游托管平台才 502；
- 后端与前端各拼一次「检查失败:」导致 toast 双前缀。

## 现状（arch-weekly-11-ljt 实测）
- `src/domains/mgmt/routes/repos.py:1228` `check_repo_name()` 仅 :1242 校验 `owner` 空间路径，`name` 无正则、无长度上限；
- 双前缀：`repos.py:1274`、`repos.py:1280` 后端拼 `f"检查失败: {…}"`，前端 `src/api/frontend/shell/locales/zh/repos.json:136`（`en` 同构）`err_check_failed` 再包一层；
- create 内联正则 `repos.py:1351`（name）/`:1359`（path）各写一份 `^[\w\-\.]+$`，与 check-name 不一致；
- `src/domains/mgmt/user/repo_naming.py` 在本基线不存在，需新增。

## 范围
### In Scope
- 新增公共名称校验工具（正则 + 长度上限 100），check-name 与 create 共用；
- check-name 进入 token/上游调用前先校验，非法输入直接 400 + 裸原因（0 次上游调用）；
- create 复用同一工具，删除两处内联正则；
- 错误前缀只保留一层：后端只回裸原因，前端统一加前缀（模板一键创建流程补齐单层前缀）。

### Out of Scope
- 不调整 `owner` 空间路径校验（`_valid_space_path`）逻辑；
- 不改动上游平台侧命名策略；
- 不新增/调整其它接口字段契约。

## 验收（同 Issue）
- [ ] 含空格 / 中文 / 200 字符名称 → check-name 400 且 body 说明原因，上游调用 0 次；
- [ ] 合法名称行为不变（200 + `exists` 字段语义不变）；
- [ ] toast 只出现一次「检查失败:」前缀（zh/en 一致）；
- [ ] create 与 check-name 同输入同结果（同工具驱动）；
- [ ] 现有测试通过，新增测试覆盖上述边界。
