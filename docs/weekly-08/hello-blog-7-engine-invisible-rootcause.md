# 仓库级引擎在 Workers 设备不可见 — 根因与修复方案

- 日期: 2026-10-19
- 现场: 用户（user_id=<X>，组织 <ORG_ID> 社区博客团队）把 worker 凭据 `<CRED_NAME>`（id=<CRED_ID>）从个人级（user scope）改为仓库级（repo scope，gitcode:<owner>/<repo>）后，Workers 设备页看不见该设备，场景引擎下拉也选不到其 4 个引擎（opencode / codearts / hermes / hwcloud）。
- 结论：**转换接口 change_credential_scope 末尾一句"无条件清空 owner_id"把刚写入的仓库级 owner_id 擦掉了，所有以 owner_id 判定仓库归属的可见性/查询链路随之失效。**

## 1. 证据链

### 1.1 DB 落库结果

runtime_credentials id=<CRED_ID>:

| 字段 | 值 | 判定 |
|---|---|---|
| scope | repo | 正确 |
| repo_key | gitcode:<owner>/<repo> | 正确 |
| owner_name | <owner> | 正确 |
| org_id | <ORG_ID> | 正确 |
| **owner_id** | **''（空）** | **错误，根因字段** |

agent_runtimes 4 行（<RT_ID_RANGE>）：scope=repo、repo_key 正确，但 **owner_id=''**（注册时从凭据行复制，同样为空）。引擎本身 status=online、心跳新鲜、probe ok —— worker 侧完全正常，纯服务端可见性问题。

### 1.2 代码缺陷（src/api/routes/settings.py, change_credential_scope）

现网部署树与 dev 分支一致，语句顺序：

```python
c.execute("UPDATE runtime_credentials SET scope=?, repo_key=? WHERE id=?", ...)

if scope == "repo" and repo_key:                      # ← 修复：补写 owner_id
    c.execute("UPDATE runtime_credentials SET owner_id=? WHERE id=?", (repo_key, cid))
c.execute("DELETE FROM engine_capabilities WHERE ...")  # 删旧 scope 引擎行
c.execute("DELETE FROM agent_runtimes WHERE ...")
c.execute("UPDATE runtime_credentials SET owner_id='' WHERE id=?")   # ← BUG：无条件执行
```

最后一句是 repo→user 方向清理脏 owner_id 用的，但没有 `scope=='user'` 条件，
user→repo 方向同样执行，把上一步刚写入的 `owner_id=repo_key` 覆盖回空串。
提交后落库 owner_id 恒为空 —— 修复被这句完全抵消（修复顺序颠倒，晚写的覆盖早写的）。

### 1.3 owner_id 为空导致的三条断链

1. **Workers/Engines 页可见性**（`_make_visible_credential_fn`，repo 分支）：
   `_rk = from_repo_key(cowner_id) or from_repo_key(crepo_key)`。
   - 仓库上下文（前端 runtimes.js 会带 `&repo=<owner>/<repo>`）→ `repo_filter` 非空 → 要求 `_rk == repo_filter`，空串永不相等 → **连 owner 本人都看不见**；
   - 不带 repo 参数 → owner 本人靠 `cowner_name == uname` 仍可见，但**同仓库其他成员**走 `_rk in user_repos`，失败 → 看不见。
2. **`_build_runtime_devices` 兜底参数缺失**：调用 `_visible_credential` 时只传 4 参（cid, scope, owner_name, owner_id），未传 `crepo_key` 兜底参数（agent-runtimes 循环那处传了）→ 1 中"回退用 cred.repo_key"的防御逻辑在设备视图根本不生效。
3. **场景引擎下拉**（`engine_select.query_engines(repo_key, "repo")`）：SQL 硬条件 `WHERE rc.scope='repo' AND (rc.owner_id=? OR rc.owner_id=?)`（repo / gitcode:repo 两种形式），owner_id 空 → 返回 0 行 → 场景编辑弹窗无仓库级引擎可选。

另外引擎行 owner_id 也为空 → 注册 upsert 四元组 (credential_id, name, scope, owner_id) 与正确值不匹配，修复凭据后若不清理旧行，worker 下一次注册会插一套新行造成重复引擎。

## 2. 修复方案

### 2.1 代码修复（治本）

**A. change_credential_scope：清空语句加方向条件**（settings.py ~1288 行）

```python
# 仅 repo→user 方向需要清残留 owner_id；user→repo 方向保留第 2 步写入的仓库全名
if scope != "repo":
    c.execute("UPDATE runtime_credentials SET owner_id='' WHERE id=?", (cid,))
```

**B. _build_runtime_devices：补传兜底参数**（settings.py ~853 行）

```python
if not visible_credential(cid, cred.get("scope", "global"), cred.get("owner_name", ""),
                          cred.get("owner_id", ""), cred.get("repo_key", "")):
```

与 agent-runtimes 循环处对齐，owner_id 为空时回退 cred.repo_key 判定仓库归属。

**C. query_engines repo 查询加 repo_key 兜底**（engine_select.py ~539 行）

```sql
AND (rc.owner_id=? OR rc.owner_id=? OR rc.repo_key=? OR rc.repo_key=?)
```

使历史脏数据（owner_id 空但 repo_key 正确）的仓库级引擎仍可被场景选中。

> C 是防御性兜底；A 才是根因修复。B/C 保证存量脏数据不再造成"设备消失"类故障。

### 2.2 存量数据修复（一次 SQL）

```sql
-- 1) 修凭据行：补 owner_id（与注册路径一致，写带前缀形式，query_engines 两种都匹配）
UPDATE runtime_credentials
   SET owner_id='gitcode:<owner>/<repo>'
 WHERE id=<CRED_ID> AND scope='repo';

-- 2) 清掉 owner_id 为空的引擎行及其能力行（避免重新注册后四元组不匹配产生重复引擎）
DELETE FROM engine_capabilities
 WHERE runtime_id IN (SELECT id FROM agent_runtimes
                       WHERE credential_id=<CRED_ID> AND (owner_id IS NULL OR owner_id=''));
DELETE FROM agent_runtimes
 WHERE credential_id=<CRED_ID> AND (owner_id IS NULL OR owner_id='');
```

worker 心跳周期内（/register 幂等 upsert）自动按新 owner_id 重建引擎行，version/cli_path/local_skills 全量重报，零数据丢失。场景 nodes_json 里引用的旧 runtime_id 需检查：

```sql
SELECT scene_id, nodes_json FROM devops_pipeline_scene
 WHERE repo_key='gitcode:<owner>/<repo>' AND is_active=1;
```

若 engine_runtime_ids 引用了旧 id，重建后新 id 不同，需回写（或先改场景绑定为空=广播，再让用户在编辑器重选）。

### 2.3 验证步骤

1. 执行 2.2 SQL 后，等一次 worker 心跳（分钟级），确认引擎行 owner_id 已正确写入。
2. 用户在仓库上下文打开 Workers → 设备可见、scope 标签=仓库级。
3. 组织内另一成员（需 user_repos is_join=1）同页确认可见。
4. 场景编辑弹窗 → 4 个引擎可选。
5. 正反向回归：user→repo、repo→user 各转换一次，落库 owner_id 分别为 repo_key / 空，且不产生重复引擎行。

### 2.4 部署

- 改动文件：settings.py、engine_select.py（A/B/C 三处）
- 部署方式：将改动文件同步至部署目录，重启服务
- 风险评估：A 只影响转换接口（低频管理操作）；B/C 为读路径加兜底，不改变 owner_id 正常时的行为；2.2 的 DELETE 范围被 credential_id+owner_id 双重限定，不波及其他设备。
