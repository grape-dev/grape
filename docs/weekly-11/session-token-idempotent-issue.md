# 会话凭据（GRAPE_SESSION_TOKEN）缺失与幂等修复

## 1. 现象

A2A 补发任务在执行期自述：

```
前置检查未通过：任务级会话凭据未注入且平台服务端不可达，
Issue #1 的 A2A 补发处理未执行
```

具体表现三件事同时发生：

1. 子进程 `environ` 里没有 `GRAPE_SESSION_TOKEN`，agent 只能退回 `GRAPE_AUTH_FILE`/config，
   `issues detail` / `tasks complete` / 评论回复全部 `Missing token` 或超时；
2. 同一任务的 `agent_tasks.env_extra`（快照）里**却有** `GRAPE_SESSION_TOKEN`（长度正常、前缀 `tk_`）；
3. 日志报「服务端不可达」：`GRAPE_SERVER_URL=http://<公网 IP>`，而执行节点访问该地址 80 端口返回 000
   （同机 `127.0.0.1:80` 与内网 `192.168.0.x:80` 均 200）。

## 2. 根因

### 2.1 同一 session_key 被签发两次 → 唯一索引冲突（代码缺陷）

| 位置 | 行为 |
|---|---|
| `src/domains/runtime/engine/service.py` 入队阶段 | `issue_session_token(session_key=task_id)` 首次落库 |
| `src/domains/runtime/pipeline/event_pipeline.py` 派发阶段（旧版） | 对同一 `session_key` **再签一次** |
| `src/domains/runtime/engine/direct_executor.py` 派发阶段（旧版） | 同上（且文件已 import 复用原语，却仍直接签发） |

第二次插入命中部署库上的 `UNIQUE(session_key)`（索引名 `uniq_session_key`）→ `1062 Duplicate entry`；
该异常在派发路径被 `except` 吞成一条 warning「task continues」，于是**快照有 token、进程环境没有 token**。

> 派发期不新签这一点，主线已改为 `service.py::session_token_for_execution()`（复用/同步任务快照，
> 注释写明「绝不制造未落库 token，否则运行时注入值与 shell 环境快照再次漂移，正是 #990 的症状」）。
> 落后于主线的部署（旧 commit）仍会走到「再签一次」。

### 2.2 签发/轮换原语非幂等（代码缺陷，主线仍未修）

```python
# 旧实现：只把旧行 expires_at 缩短，然后裸 INSERT
with _any_tx() as c:
    c.execute("UPDATE session_api_tokens SET expires_at=? "
              "WHERE session_key=? AND CAST(expires_at AS INTEGER) > ?", (int(now), session_key, int(now)))
    c.execute("INSERT INTO session_api_tokens (...) VALUES (...)", (...))
```

`session_token_insert`（`issue_session_token` 底层）同样是裸 `INSERT`。
配合 `UNIQUE(session_key)`，同一 key 第二次必然冲突 —— 这是「单行语义 vs 多行实现」的矛盾，
不是索引错、也不该删索引（该约束本身防止重复行是正确的）。

### 2.2.1 第一刀修法及其残留缺陷（rowcount 判据）

第一刀把上面的实现改成「UPDATE 全字段 → rowcount>0 视为命中 → 否则 INSERT」。
这段在 SQLite 上成立，在 **MySQL 上不成立**：

```
MySQL:   affected rows = 实际**改变**的行数  → 与旧行完全同值重写时 = 0
SQLite:  rowcount     = **匹配**的行数        → 同值重写时 = 1
```

于是 MySQL 上「同值重写」被误判成「未命中」→ 落到 INSERT → 撞 `uniq_session_key` → 1062。
SQLite 单测永远是绿的，正好把这条藏住（第一刀四条用例全绿，线上仍会炸）。
更糟的是它把「存在性」这件事交给了 UPDATE 的返回值，而存在性本来就有唯一键可以回答。

### 2.2.2 派发期仍在二次签发（未复用快照）

第一刀只改了写入原语，派发链没动：`event_pipeline.py` 与 `direct_executor.py` 仍然调用
`issue_and_inject_session_token_safe` → 底层 `issue_session_token` → **每次都新签一枚**。

后果不是 1062（原语已幂等），而是**快照漂移**：入队时写进 `agent_tasks.env_extra` 的那枚
token 被派发时覆盖成新的，快照里留下 stale 值，只能靠后续 `sync_task_session_token`
轮换回写才能收敛，中间存在 401 窗口。#990 spec 原文要求 direct 执行「复用快照中的有效
token，绝不制造未落库 token」—— 这条没落地。

### 2.3 服务端基址取到公网地址（部署配置）

任务 env 的 `GRAPE_SERVER_URL` 由 `get_server_base_url()` 解析，优先级：

```
① configs/server-config.json（本部署不存在）
② meta.server_base_url   ← 真源在 **runtime 域库**（不是 mgmt 域库）
③ webhook_url 去掉 /webhook 后缀
④ http://localhost:{GRAPE_PORT} 兜底
```

实际值为公网 IP；执行节点（另一台机器）访问该公网地址不通（NAT 回环/安全组），
而 `agent.py` 的「本机覆盖」依赖进程 env 的 `RUNTIME_SERVER_URL`，四个进程的 environ 里都没有该变量，
覆盖从未触发 → 派生出的 agent 全部指向不可达地址。

## 3. 证据（现场实测）

```
· 表约束：grape_lfr_mgmt.session_api_tokens 有 uniq_session_key = UNIQUE(session_key)
          （create_time 2026-11-18 00:21:49；同库 grape_lfr_runtime / grape_dev_mgmt 只有
           UNIQUE(token_sha256)，故 1062 只发生在 mgmt 库这张表）
· DDL：代码里 db_setup.py 的建表语句 session_key TEXT NOT NULL 无 UNIQUE，
       全仓 grep uniq_session_key = 0 命中 → 该索引是部署/迁移侧手工加的
· 实测复现：同一 session_key 第 1 次 INSERT ok，第 2 次 → ERROR 1062 Duplicate entry
· 快照 vs 环境：t_d7433336cbf1 的 env_extra 含 "GRAPE_SESSION_TOKEN": "tk_…"（997 字节 JSON），
  而执行该仓 coding 任务的 agent 进程 environ 中 GRAPE_SESSION_TOKEN 长度 = 0
· 日志：多份日志累计出现 "uniq_session_key" 1062 记录（补丁前；补丁+重启后单文件 0 新增）
· MySQL 语义实测：`UPDATE ... SET <同值> WHERE session_key=?` → `ROW_COUNT()` = **0**；
  改值 → 1（即 rowcount 判存在性在 MySQL 上不可用）
· 旧逻辑红证据（真库 grape_lfr_mgmt，临时 key，跑完已清）：
    第 1 次 INSERT ok → 同值 UPDATE rowcount=0 → 再 INSERT
    → IntegrityError (1062, "Duplicate entry '…' for key 'session_api_tokens.uniq_session_key'")
· 部署库形态不一致（同一张表两种形态，这是缺陷能长期潜伏的原因）：
    grape_lfr_mgmt: session_key = varchar(100) + uniq_session_key(UNIQUE)  + ix_sat_kind_key
    grape_dev_mgmt: session_key = text        + 只有 UNIQUE(token_sha256)，同 key 已有 1 组重复行
```

## 4. 修复

### 4.1 原语：先 INSERT，撞唯一键才降级（已在本分支落地）

`src/domains/mgmt/public/__init__.py`

```sql
INSERT INTO session_api_tokens (session_key, repo_key, agent_type, user_id,
                                token_sha256, expires_at[, kind])
VALUES (?,?,?,?,?,?[,?])
-- 仅当数据库拒绝该行（唯一键冲突）时：
UPDATE session_api_tokens SET repo_key=?, agent_type=?, user_id=?, token_sha256=?,
       expires_at=?[, kind=?] WHERE session_key=?
```

要点：

- **多枚并存是原设计**：建表只有 id 主键；`ix_sat_kind_key(kind, session_key)` 非唯一；
  `set_expiry` / `shrink_active` / `delete_by_session` 全是按 key 批量操作 —— 无唯一索引的库
  （如 grape_dev_mgmt）上同 key 两次签发就该是两行；
- **降级只为兼容既有索引**：部署侧手工建的 `uniq_session_key`（grape_lfr_mgmt，2026-11-18）
  会让 INSERT 抛唯一键冲突，此时就地覆盖该行，调用方语义不变；
- **判存在性仍不得用 rowcount**（MySQL 计改变行数=0、SQLite 计匹配行数=1，前者掩盖线上 1062）；
- **返回契约不变**：落库后 `SELECT id ... WHERE session_key=? AND token_sha256=?`。

### 4.2 列宽对齐（不是建唯一键）

```sql
-- DDL（src/api/db_setup.py）：唯一键列不能是 MySQL 的 TEXT 类型
session_key VARCHAR(191) NOT NULL      -- 原 TEXT NOT NULL

-- 迁移 202611210004 session-token-key-column-width（kind=safe, domains=("mgmt",)）
MySQL:  ALTER TABLE session_api_tokens MODIFY session_key VARCHAR(191) NOT NULL
        仅此一步 —— 不去重、不建 uniq_session_key（早期版本做过，已撤销；见 §9）
```

为什么仍要对齐列宽：部署侧可能存在手工建的 `uniq_session_key`，而 MySQL 不允许在 TEXT
上建唯一索引 —— 列宽不改会与该索引形态冲突。但代码**不主张**唯一性。

### 4.3 失败可见性（主线已有，本分支沿用）

`service.py::_inject_session_token` 在签发失败时：

```
_persist_task_diagnostic(task_id, {"error_code": "SESSION_TOKEN_ISSUE_FAILED", "reason": ...})
log.error(... "continuing without GRAPE_SESSION_TOKEN, CLI falls back to GRAPE_AUTH_FILE/config")
```

即诊断会追加进 `agent_tasks.errors`（同 error_code 幂等覆盖，最多 5 条），不再只是一条 warning。
保留「继续执行」而不是 fail-fast：agent 侧仍有 `GRAPE_AUTH_FILE`/config 回退，
真正致命的是**静默**，不是继续。

### 4.4 部署（已按内网基址修正）

```
grape_lfr_runtime.meta.server_base_url: http://<公网 IP>  →  http://192.168.0.194
（内网地址对本机四进程与远端 worker 均可达；webhook 回调走独立键 webhook_url，不受影响）
```

### 4.5 派发链：复用快照，不再二次签发（新增）

`src/domains/runtime/engine/service.py`

```python
reuse_or_issue_session_token(env, *, session_key, repo_key, agent_type,
                             user_id=0, task_id="", scope="dispatch",
                             context_log_level="warning") -> None | dict
#   内部走 session_token_for_execution(task_id, ...)：
#     快照 token 有效（session_key/repo_key/agent_type 匹配且未过期）→ 原样复用，不轮换
#     快照失效/作用域不匹配 → 同一事务内作废旧 token + 签发新 token + 回写 agent_tasks.env_extra
#     任务行存在但同步失败 → 返回空串（**不**另签未落库 token）
#   成功：env["GRAPE_SESSION_TOKEN"]=token 且 env["GRAPE_CLI_MODE"]="WORKER"
#   失败：记 SESSION_TOKEN_ISSUE_FAILED / SESSION_TOKEN_CONTEXT_MISSING 到 agent_tasks.errors
#        + log.error，env 不注入（保留 #2790 的可见性口径）

reuse_or_issue_session_token_safe(env, *, log_scope, **kw)   # 诊断钩子异常不阻断派发
```

两个派发点改为调用它（替换 `issue_and_inject_session_token_safe`）：

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `src/domains/runtime/pipeline/event_pipeline.py` | 派发前注入 | 每次新签一枚 | 复用快照 |
| `src/domains/runtime/engine/direct_executor.py` | 执行前注入 | 每次新签一枚 | 复用快照 |

## 5. 验收

```
· tests/runtime/test_session_token_idempotent.py 8 例全绿：
    rotate×2 / issue×2 / rotate+insert 混用           → 不抛
    有 uniq_session_key 的库：同 key 两次               → 1 行（撞键降级）
    无唯一索引的库：同 key 两次                         → 两行并存、两枚都有效（原设计）
    源码级回归锁：原语不含 rowcount 判存在性、裸 INSERT 优先、含 _is_unique_conflict 降级
    translate_sql 把 ON CONFLICT … DO UPDATE 翻成 ON DUPLICATE KEY UPDATE … VALUES(col)
· tests/runtime/test_session_token_reuse_snapshot.py 5 例全绿：
    快照有效 → 复用，issue_session_token 调用次数 = 0
    快照同步返回空 → SESSION_TOKEN_ISSUE_FAILED 且 env 不含 GRAPE_SESSION_TOKEN
    上下文缺失 → SESSION_TOKEN_CONTEXT_MISSING
    同步抛错 → SESSION_TOKEN_ISSUE_FAILED（reason 带原始异常）
    safe 封装吞掉钩子异常，不阻断派发
· 真库实测（grape_lfr_mgmt，临时 key，跑完已清）：
    新实现：同值 insert×2 + rotate×2 → 无异常，行数 1
    旧逻辑：同值 UPDATE rowcount=0 → INSERT → 1062（红证据，说明缺陷真实存在）
· 现网观察：日志中 "Duplicate entry … uniq_session_key" 计数归零
· 任务验证：`/proc/<pid>/environ` 能读到 GRAPE_SESSION_TOKEN，且
  任务 env 的 GRAPE_SERVER_URL 为内网地址（curl /api/health = 200）
```

## 6. 边界

- **不再**说「不改表结构」：本次新增迁移 202611210004（session_key 改 VARCHAR(191) + 去重 +
  补建 `uniq_session_key`）。改的是**补齐约束与其列类型**，用途是让 upsert 有键可命中；
  不删任何既有约束、不改语义、不改主键；
- 入库仍是 sha256，明文只经 env 一次性下发；
- 不动 auto-repair / 补偿触发链，只保证「拿得到 token、回得去连」；
- 派发期「不再新签」的改动属主线既有修复，落后部署需要同步主线才能一并受益。

## 9. 修正记录（2026-11-21 晚）：唯一性不是契约，单行语义已还原

本文 §3–§5 记录的修法里，有一条把「某一部署手工加的索引」升成了代码语义，已撤销：

```
当时做法   session_token_insert / rotate → INSERT ... ON CONFLICT(session_key) DO UPDATE
           + 迁移 202611210004（去重 + CREATE UNIQUE INDEX uniq_session_key + TEXT→VARCHAR(191)）
问题       原设计允许多枚并存 —— 建表只有 id 主键；ix_sat_kind_key(kind, session_key) 非唯一
           （information_schema NON_UNIQUE=1）；set_expiry / shrink_active / delete_by_session
           全是 WHERE session_key=? 的批量写法；读侧只有 by_sha，没有「按 key 取一枚」。
           无条件 upsert 把「有索引的库」的物理约束变成了「所有库都必须单行」，
           抹掉了 chat 的 conv 级 key 依赖的那种形态。
现在做法   裸 INSERT 优先；撞唯一键（SQLite UNIQUE constraint / MySQL 1062）才降级 UPDATE；
           迁移只留列宽对齐（TEXT→VARCHAR(191)，因为部署侧可能的 uniq_session_key 要求定长列），
           不再去重、不再建索引；判存在性仍不得用 UPDATE 的 rowcount。
已建索引的库   保留现状即可（代码按「可能存在的物理约束」兼容）；
           要还原原设计形态需人工：ALTER TABLE session_api_tokens DROP INDEX uniq_session_key
```

因此 §4「判据换成唯一键」、§5「不删索引、把 UNIQUE(session_key) 补进代码」这两条结论**不再成立**，
以本节和 `clone-endpoint-single-source-design.md` §9.10.1 为准。#990 的真实最小修法只是
「派发前只签发一次、复用快照」，唯一键从来不是必需的。
