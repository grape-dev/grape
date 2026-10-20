# 方案设计: Issue #376

## 总体方案
两条线并行修复：① SQL 方言兼容——派生表包裹消除同表子查询（MySQL 1093）；② 票据时序——校验/消费拆分，消费移到业务事务成功后。

## 1. 派生表包裹（消除 MySQL 1093）

MySQL 1093 禁止 UPDATE/DELETE 目标表直接出现在子查询 FROM 中；包裹成 `(SELECT ... FROM (SELECT ... FROM <表> ...) t)` 后目标表仅出现在派生表内部（物化），SQLite 3.x / MySQL 8.0 双兼容，语义不变。

### transfer_business_data()（src/auth/grape_account_db.py）
```sql
UPDATE {table} SET {col}=? WHERE {col}=? AND repo_key NOT IN
  (SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)

DELETE FROM {table} WHERE {col}=? AND repo_key IN
  (SELECT repo_key FROM (SELECT repo_key FROM {table} WHERE {col}=?) t)
```

### latency.py 超限清理
```sql
DELETE FROM api_latency_log WHERE id IN
  (SELECT id FROM (SELECT id FROM api_latency_log ORDER BY id ASC LIMIT {excess}) t)
```

### table_maintenance.py（同类统一）
SQLite 分支 `DELETE FROM task_messages WHERE id IN (...) LIMIT` 同步包裹（仅 SQLite 执行，统一写法防维护误复制）。

### 翻译层佐证
`src/mysql_compat.py` 已有 1093 派生表包装逻辑（`AS __derived`）；包裹后 SQL 直达 MySQL 不再触发 1093 形态，由翻译层断言测试覆盖。

## 2. 票据校验/消费拆分（grape_account_db.py + grape_auth.py）

- `validate_binding_ticket(ticket, user_id, consume=True)`：默认保持旧行为（查+删）；`consume=False` 只校验不删。
- 新增 `consume_binding_ticket(ticket) -> bool`：独立 DELETE，幂等（不存在返回 False）。
- transfer-bind 端点（src/api/routes/grape_auth.py）：
  1. `validate_binding_ticket(..., consume=False)` 校验（user/origin/过期校验仍生效）；
  2. 业务事务执行；失败路径 raise/rollback 时票据不删（剩余有效期内可重试）；
  3. `_ga_db.commit()` 成功后 `consume_binding_ticket(ticket)` —— 一次性消费语义保留（成功后不可重放）。

并发安全：重放窗口=事务执行期（秒级），且 validate 阶段 initiating_user_id 与 JWT 会话强校验；维持 db_lock 语义不变。

## 3. 测试方案
- 源码 SQL 文本断言（tokenize 还原 f-string 隐式拼接）：派生表包裹形态 + 无直接同表子查询。
- 真实 SQLite 执行：user_repos 冲突合并/无冲突/空源三场景语义不变；latency trim 语义。
- 票据两步语义：consume=False 不删、consume 删除、幂等、默认旧行为、user 不匹配不消费。
- API 级（TestClient + 真实票据）：首次 500 → 票据仍有效 → 同票据重试 200 → 成功后重放 410。
- 翻译层：迁移 SQL 经 translate_sql 后无 1093 直接形态、占位符数量不变。
