# 跨域连接归属（根因修复）—— 设计与运维手册

## 1. 事故现象

```
resolve_exec_token('gitee:yang-aiwu-new/demo') → stage=undecryptable
```
同一仓库在两库里各有一条 `grape_t_repo_credentials` 行：
- `grape_dev_mgmt`：5 条 gitee 行，`kid=derived`，解不开（InvalidTag）
- `grape_dev_insight`：3 条，`kid=k1`，能解出 32 位令牌

新代码把“仓库级凭据行”当权威 → 读到毒行 → 显式失败；此前它跳过 rc 行落平台 bot，所以 gitee 能 push，现在被挡住。

## 2. 根因

`grape_t_repo_credentials` 归 **insight**（`configs/table_ownership.json`）。但 insight 的读写口在缺 `GRAPE_DSN_INSIGHT` 时会**静默回落进程主库**：

```python
# 旧代码（src/domains/insight/public/__init__.py）
if db_registry.uses_own_dsn("insight"):
    ...  # insight 连接
else:
    session_manager.db  # ← 进程主库；mgmt 进程里就是 mgmt 库
```

于是 mgmt 进程：
- 读：读到 mgmt 库里的同名表（毒行）
- 写：把 rc 行写进 mgmt 库，并用 mgmt 进程那套派生密钥加密（`kid=derived`）
- insight 侧（k1）与 mgmt 侧（derived）互相解不开

同类回落存在于 `mgmt/user/db.py::_MgmtDbProxy`（D1），以及 ORM 入口 `get_domain_engine`。

## 3. 修复：全仓唯一的连接归属判定

```python
# src/kernel/db_registry.py
route_of(domain) -> "local" | "domain"
  单库拓扑（GRAPE_DOMAIN_LIB 未开）        → "local"
  本域进程（domain == current_domain）     → "local"   # 零回归，不另开连接
  跨域 + 有 GRAPE_DSN_<DOMAIN>             → "domain"
  跨域 + 缺 DSN                            → 抛 DomainUnavailable（绝不回落主库）

cursor_for(domain, write=False)   # 域连接 或 主库（带进程锁 + 显式提交）
transaction_for(domain)           # 域连接真事务 或 主库锁 + 提交/回滚
```

调用方：

| 位置 | 旧 | 新 |
|---|---|---|
| `insight/public._any_cursor/_any_tx` | 自写 `uses_own_dsn ? DSN : 主库` | `cursor_for("insight")` / `transaction_for("insight")` |
| `mgmt/user/db._MgmtDbProxy` | 同上（三处分支） | `route_of("mgmt")`（缺 DSN 抛，不回落） |
| `kernel/persistence/engine.get_domain_engine` | 缺 DSN 回落主库 | 先问 `route_of`；跨域缺 DSN 抛 `DomainUnavailable` |

异常统一：`db_registry.DomainUnavailable(domain)`；`persistence.engine.DomainEngineUnavailable` 保留为别名，避免调用点批量改。

## 4. 启动自检

`assert_role_database(role)`（四个进程 lifespan 都已调用）追加：

```
三库拓扑缺任一 GRAPE_DSN_<DOMAIN> → ERROR 日志（列出变量名与受影响域）
  · 默认：继续启动（跨域访问会显式失败，不会写错库）
  · GRAPE_3DB_STRICT_DSN=1：拒绝启动（SystemExit 6）
/api/health 的 db_registry.health() 增加 _missing_dsns 字段
```

## 5. 数据层善后（先备份，可回滚）

**不要重写 mgmt 的 5 行**：rc 表归 insight，把值写回 mgmt 等于把权威数据放到非 owner 库，下次还会分裂。

```sql
-- 0) 备份（两台都做）
--    mysqldump ... grape_dev_mgmt    grape_t_repo_credentials > rc_mgmt_<ts>.sql
--    mysqldump ... grape_dev_insight grape_t_repo_credentials > rc_insight_<ts>.sql

-- 1) 只读盘点：mgmt 里哪些 repo_key/cred_type 是 stray（不在 insight 期望集合内）
SELECT repo_key, cred_type, updated_at FROM grape_t_repo_credentials
 WHERE repo_key LIKE 'gitee:%';

-- 2) 删 stray 行（按 repo_key 精确删，先 SELECT COUNT 断言）
DELETE FROM grape_t_repo_credentials
 WHERE repo_key IN (<盘点到的那 5 个>) AND cred_type='bot';
-- 期望 ROW_COUNT()=5；不符立刻 ROLLBACK

-- 3) 补齐：insight 缺的仓用平台写口（k1 加密）补行，不手写 SQL 加密

-- 4) 复测
--    resolve_exec_token('gitee:yang-aiwu-new/demo') → level=rc_bot, stage=''
--    换券 200；建树 refs≠0；三个 gitee 仓 push 正常
```

## 6. 用例

```
tests/kernel/test_domain_route_strict.py
  S1 单库 → local          S2 本域 → local
  S3 跨域 + DSN → domain   S4 跨域 + 缺 DSN → 抛 DomainUnavailable
  S5 启动自检列出缺项      S6 insight 写口缺 DSN → 抛且不碰进程主库（毒行回归）
  S7 mgmt 代理缺 DSN → 抛且不碰进程主库
  S8 棘轮：域 public 不得再自写 uses_own_dsn 回落
```

## 7. 部署检查清单

```
1) 四个 unit 的 env 里 GRAPE_DSN_MGMT / GRAPE_DSN_RUNTIME / GRAPE_DSN_INSIGHT 都在
2) 启动日志无 "[db-guard] 三库拓扑缺少 …"
3) /api/health 的 db_registry health 无 _missing_dsns
4) 数据善后按 §5 执行并复测三个 gitee 仓
```