# Plan: Issue #827

## 切片拆解（TDD 红/绿分提交）

### Slice 1: ensure_issue_task 保留 user_id + 写入 created_by
**改动文件**: `src/domains/runtime/issues_center.py`（P1 模块归位后的 canonical 实现；
`src/api/issues_center.py` 仅为兼容别名，改动落在 canonical 侧）
**测试 seam**: `ensure_issue_task` 公开接口——建卡→复用→assignee 变更→user_id 行为

**修改点**:
1. assignee 变更分支：新解析 `user_id` 为空/0 时不覆写已有 `_existing_user_id`（保留原归属）；
   D105 起 `issue.user_id` 为 TEXT（uuid 字符串）口径，一律 str 归一比较，不用数值大小判定。
2. INSERT 分支：`created_by` 值 = `creator or sender or target`（与 #805 创建人一等数据化口径并存）。
3. 复用分支：`created_by` 为空且 `creator`/`sender`/`target` 任一非空时回补（#805 幂等语义保留：
   仅填空值，绝不覆盖已有创建人）。

**红测试**: assignee 变更为未注册用户后 user_id 不为 0（复现 bug 症状）。
**绿实现**: 保留逻辑 + created_by 写入。

### Slice 2: 「我的需求」过滤口径扩展
**改动文件**: `src/domains/runtime/routes/issues.py`（`my_issues` 端点迁入 runtime 域后的 canonical 路径）
**测试 seam**: `my_issues` 端点——user_id=0 但 created_by/assignee 命中时可见

**修改点**:
1. `my_issues` 端点：按当前用户 user_id 反查平台登录名（复用既有 `_get_gitcode_user_by_user_id`，
   身份域 `external_identities` 优先、`unified_user_mapping` 存量兜底），不新增重复查询实现。
2. 过滤条件由 `user_id=?` 改为 `(user_id=? OR created_by=? OR assignee=?)`；反查不到登录名时
   退回原单一 `user_id` 口径，不放大可见范围。

**红测试**: user_id=0、created_by=当前用户名的卡片在 /mine 返回。
**绿实现**: 扩展过滤口径。

### Slice 3: 回填脚本增强 created_by 回退
**改动文件**: `scripts/backfill_issue_user_id.py`
**测试 seam**: `candidates()` / `apply()`——assignee 不可映射但 created_by 可映射时列入候选

**修改点**:
1. `_SQL_CANDIDATES` 增加 `created_by` 回退源（COALESCE assignee → created_by）。
2. `apply()` 同步增强。

**红测试**: assignee 不可映射、created_by 可映射的行出现在候选列表。
**绿实现**: 回填 SQL 增强。

## 风险与缓解
- 过滤口径扩展影响性能 → created_by/assignee 为低基数 TEXT 列，OR 匹配成本可控；必要时加索引。
- created_by 回填误判 → 仅回填可映射（unified_user_mapping 命中）的行；不可映射跳过。

## rebase 适配记录（base 由 weekly-10-zhangjian 改为 arch-weekly-11）
- 模块归位：`src/api/issues_center.py` / `src/api/routes/issues.py` 的实现迁至
  `src/domains/runtime/` 下（旧路径为兼容别名），本 PR 改动相应落到 canonical 模块。
- D105 归属校准：base 已有「0/默认账号 → 解析值」校准块与 #805 created_by 幂等补写块，
  与本 PR 的「保留既有 user_id」「created_by 回源」不冲突，二者并存；本 PR 只补
  「新解析值为空/0 时保留原值」与「creator 缺失时回退 sender/target」两处缺口。
- 测试布局随 base 调整为 `tests/runtime/test_issue_827_*.py`，导入路径改用 kernel/mgmt 域 canonical 模块，
  user_id 断言按 TEXT 口径 str 归一。
