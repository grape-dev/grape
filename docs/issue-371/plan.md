# Plan: Issue #371 — 个人引擎改为仓库级后在 Workers 设备页不可见

> Coding Agent | Branch: feat/issue-371 → weekly-08-ljt | 2026-10-19

## 修复方案

### 改动 1：`change_credential_scope`（src/api/routes/settings.py:1241-1291）

**问题 A — org_id 未同步**：
- user→repo 转换：repo scope 的 org_id 应对齐到「仓库所属组织 org_id」
  （`get_org_id_by_repo_key(repo_key)`），仓库无组织归属时置 0。
- repo→user 转换：user scope 的 org_id 应对齐到「当前操作用户所在组织」。
  `change_credential_scope` 用 `require_no_session_token`（无 org_id 注入），
  需从 `managed_repos.org_id` 或用户个人组织解析。简化方案：user scope org_id 置 0
  （个人 worker 在任何组织视图都由 `_visible_credential` 的 owner 判定收敛，与 #338 一致）。

**问题 B — line 1288 无条件清 owner_id**：
- 当前逻辑：line 1268 写 `owner_id=repo_key`（repo scope），line 1288 又清空。
- 修复：repo scope 保留 owner_id=repo_key；user scope 清空 owner_id（与注册路径一致）。
- 即把 line 1288 的无条件 `UPDATE ... SET owner_id=''` 改为「仅 user scope 清空」。

**问题 C — 删旧行的 owner_id 判定**：
- line 1277-1285 的 DELETE 条件 `scope!=? OR (owner_id IS NOT NULL AND owner_id != '')` 在
  repo scope 切换时会把新 owner_id 非空的旧行也删（但旧行 scope 是旧 scope，本就该删）。
  逻辑保持不变，仅确认无副作用。

### 改动 2：`get_agent_runtimes`（settings.py:598-622）非个人组织分支

当前 `_filter_org_id > 0` 分支（line 604-607）：
```python
if _filter_org_id:
    if rt_org_id != _filter_org_id:
        continue
```
改为增加 repo scope 豁免（与 #338 个人组织分支同口径）：
```python
if _filter_org_id:
    if rt_org_id != _filter_org_id:
        _cred_early = cred_map.get(rt.get("credential_id") or 0) or {}
        _repo_scope = _cred_early.get("scope") == "repo"
        if not _repo_scope:
            continue
        # repo scope 豁免：可见性由 _visible_credential 的 owner/user_repos 判定收敛
```

### 改动 3：`get_agent_runtimes` 设备视图 cred_map 过滤（settings.py:698-700）

当前 `_filter_org_id > 0` 分支：
```python
_devices_cred_map = {k: v for k, v in cred_map.items() if (v.get("org_id") or 0) == _filter_org_id}
_devices_cached = [rt for rt in cached if (rt.get("org_id") or 0) == _filter_org_id]
```
改为增加 repo scope 豁免：
```python
_devices_cred_map = {k: v for k, v in cred_map.items()
                     if (v.get("org_id") or 0) == _filter_org_id
                     or v.get("scope") == "repo"}
_devices_cached = [rt for rt in cached if (rt.get("org_id") or 0) == _filter_org_id
                   or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
```

### 改动 4：`get_runtime_devices`（settings.py:1007-1010）非个人组织分支

当前 `_filter_org_id > 0` 分支：
```python
_local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or 0) == _filter_org_id]
cred_map = {k: v for k, v in cred_map.items() if (v.get("org_id") or 0) == _filter_org_id}
```
改为增加 repo scope 豁免（与 #338 个人组织分支同口径）：
```python
_local_runtimes = [rt for rt in _local_runtimes if (rt.get("org_id") or 0) == _filter_org_id
                   or (rt.get("credential_id") and (cred_map.get(rt.get("credential_id")) or {}).get("scope") == "repo")]
cred_map = {k: v for k, v in cred_map.items()
            if (v.get("org_id") or 0) == _filter_org_id
            or v.get("scope") == "repo"}
```

### 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| org_id 修正位置 | 切换入口同步写 + 端点豁免兜底 | 只做端点豁免保留脏数据；只做入口落库无法救历史脏数据 |
| repo scope org_id 取值 | `get_org_id_by_repo_key(repo_key)` | 仓库权威单一来源，与 managed_repos 一致 |
| user scope org_id 取值 | 0 | 个人 worker 跨组织可见性由 _visible_credential owner 判定收敛（与 #338 一致） |
| owner_id 清空时机 | 仅 user scope 清空 | repo scope 保留 repo_key 作为 owner_id（注册路径一致） |

## 风险与缓解
- **豁免逻辑扩大可见范围**：豁免仅限 repo scope，可见性最终由 `_visible_credential` 的
  `repo_key in user_repos` 判定收敛（#277 仓库隔离不回退）。测试覆盖越权场景。
- **存量脏数据**：切换入口每次切换对齐 org_id，历史脏数据随用户下次切换自愈；
  端点豁免兜底确保切换前也不不可见。
