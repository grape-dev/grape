# Plan: Issue #654 开发方案

## 实现路径（对应 Spec 四条）

### 1. register upsert 键收敛（src/api/routes/runtime.py）

- 查找键从 `(credential_id, lower(name), scope, owner_id)` 收敛为 `(credential_id, lower(name))`（多行历史脏数据时取 `ORDER BY id DESC LIMIT 1`，与 scope 切换去重的"保留 MAX(id)"口径一致）。
- 命中既有行后原地 UPDATE，同步凭据派生属性 `scope/owner_id/repo_key/org_id`（引擎 id 天然稳定）。
- 派生属性实际变化时输出结构化变更事件日志（cid、engine id、旧→新 scope/owner）。

### 2. scope 切换 owner_id 对齐（src/api/routes/settings.py）

- `change_credential_scope` 对引擎行的 UPDATE 中，user scope 的 `owner_id` 与 register 同源：按凭据 owner_name 解析 `grape_accounts.id`（解析不到回退空串，兼容老数据）。
- 凭据行本身 owner_id 语义不变（repo=repo_key / user=空，保持 #371 修复口径）。

### 3. 替换路径 capabilities/tags_user_set 迁移 + 引用 remap（settings.py + engine_select.py）

- `change_credential_scope` 的去重 DELETE 前：
  - 按名称分组找被替换行（非 MAX(id)）：存活行无 engine_capabilities 时迁移被替换行的 capabilities 行（runtime_id 改指向存活行）；存活行 tags_user_set=0 且任一被替换行 =1 时置 1。
  - 构建 `{old_rid: surviving_rid}` 映射，调用 `remap_engine_references`。
- `src/runtime/engine_select.py` 新增：
  - `remap_scene_engine_bindings(rid_map)`：场景 nodes_json 的 engine_runtime_ids 按 map 替换（cleanup 的泛化）。
  - `remap_engine_references(rid_map, cid, reason)`：统一 remap 场景 nodes、autopilot_tasks.engine_runtime_ids、devops_agent_session.runtime_id、chat_agent_session.runtime_id、agent_type.default_runtime_id、agent_type_runtime_pool.runtime_id，并输出结构化事件日志（cid、影响引擎数、旧→新 id 映射、各表命中数）。
- 默认 'all'/50 只在「无任何历史 capabilities 且未手动设置标签」时写入（register 既有逻辑不变，天然满足）。

### 4. 事务与并发

- 全部在既有 `db_lock` 事务边界内执行，沿用现状不加新锁。

## 测试方案（tests/test_issue_654_scope_engine_stability.py）

参照 test_issue_371 的 sqlite 隔离模式（直调函数 + db 断言）：

1. 复现 1000176 场景：repo 注册 → 切 user → 切回 repo → 引擎 id 不变、priority=100 保留、标签保留、tags_user_set 保留。
2. register 在 scope 变化后原地同步派生属性（不 INSERT 新行）。
3. 引擎行替换（人工制造双行脏数据）时：capabilities 迁移、tags_user_set 继承、场景 engine_runtime_ids / devops_agent_session.runtime_id / chat_agent_session.runtime_id / autopilot_tasks.engine_runtime_ids remap。
4. 变更事件日志包含 cid、旧→新 id 映射（caplog 断言）。

## 风险与缓解

- upsert 键变更影响多 worker 同名引擎并存：语义不变（每 credential+引擎名独占一行），回归 test_runtime_protocol 既有用例。
- 历史 owner_id 空值脏行：查找键收敛后命中旧行原地更新，天然兼容。
- chat_agent_session/agent_type 等表在旧库可能缺列：逐表 try/except 兜底。
