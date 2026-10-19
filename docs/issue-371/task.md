# Task Breakdown: Issue #371

> Coding Agent | Branch: feat/issue-371 → weekly-08-ljt | 2026-10-19

## 任务清单

### T1. 修复 change_credential_scope（src/api/routes/settings.py:1241-1291）
- [x] user→repo 转换：同步 org_id = `get_org_id_by_repo_key(repo_key)`（仓库无组织时置 0）
- [x] repo→user 转换：同步 org_id = 0（个人 worker 跨组织可见性由 _visible_credential 收敛）
- [x] 修复 line 1288 无条件清 owner_id：改为仅 user scope 清空，repo scope 保留 owner_id=repo_key
- [x] 提取 helper `_resolve_cred_org_id_for_scope(scope, repo_key, user)` 复用 org_id 解析逻辑

### T2. 非个人组织严格隔离分支增加 repo scope 豁免
- [x] `get_agent_runtimes`（settings.py:604-607）：rt 过滤分支增加 repo scope 豁免
- [x] `get_agent_runtimes` 设备视图 cred_map/cached 过滤（settings.py:698-700）：增加 repo scope 豁免
- [x] `get_runtime_devices`（settings.py:1007-1010）：_local_runtimes + cred_map 过滤增加 repo scope 豁免

### T3. 新增测试 tests/test_issue_371_scope_change_visibility.py
- [x] user→repo 切换后 Workers 设备页引擎可见（非个人组织视图）
- [x] repo→user 切换后引擎可见性恢复
- [x] 切换后 scope/owner_id/org_id/repo_key 落库一致
- [x] 切换后无孤儿/重复引擎行
- [x] #338 回归：个人组织视图个人设备可见
- [x] #277 回归：仓库隔离不越权（非仓库成员看不到 repo scope 设备）

### T4. 自审查 + 提交 + PR
- [x] 5 轴自审查
- [x] 原子提交
- [x] 建 PR（base=weekly-08-ljt）
- [x] Issue 评论告知
- [x] A2A 通知 QA
