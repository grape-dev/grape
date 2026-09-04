# 知识库接口优化方案设计

> 分支：fix/knowledge-id-and-auth（基于 dev 83cc3ff1）
> 日期：2026-09-01
> 状态：待实施

## 一、问题背景

### 1.1 现象

生产环境请求知识内容接口返回 400 Bad Request：

```
GET /api/knowledge/content?repo=signway%2Ffluxgate&obs_key=project-info%2Fgitcode%3Asignway%2Ffluxgate%2Fproject-capabilities.md
Status: 400 Bad Request
```

### 1.2 根因分析

调用链路：

1. 前端 `knowledge.js:201` 只传 `obs_key` 参数，不传 `file` 参数
2. 后端 `read_knowledge_content`（routes/knowledge.py:406-439）收到 obs_key，进入 OBS 分支
3. OBS 未启用或对象不存在时，回退到本地文件模式：`_file = file or obs_key`
4. 拿 obs_key 值当本地文件路径，交给 `_safe_rel` 校验
5. obs_key 值含冒号 `:`（来自 repo_key 格式 `gitcode:signway/fluxgate`），`_SAFE_REL` 正则 `^[\w.\-/]+$` 不允许冒号 → 400 "非法路径"

**本质矛盾：写入时 obs_key 天然带冒号，读取时安全校验不允许冒号。**

### 1.3 回退本地模式即使过了校验也读不到文件

obs_key 是 storage_key 格式，与本地文件路径、DB file_path 三种格式互不一致：

| 格式 | 示例 | 来源 |
|---|---|---|
| obs_key (storage_key) | `project-info/gitcode:signway/fluxgate/project-capabilities.md` | builder.py:125 |
| 本地文件路径 | `{WORKSPACE_DIR}/signway/fluxgate/project-info/project-capabilities.md` | builder.py:66-68 |
| DB file_path | `signway/fluxgate/project-info/project-capabilities.md` | builder.py:129 |

obs_key 回退到本地模式时无法正确映射到文件路径，整条回退链路是断的。

### 1.4 baseline 重复行问题

表 `agent_shared_knowledge` 没有任何 UNIQUE 约束。builder.py:134 的 `INSERT OR REPLACE` 永远不会触发 REPLACE 分支（无约束冲突）→ 每次都是纯 INSERT → 每次生成新 id。

后果：
1. DB 累积重复行——每次刷新 project-info 加 3 行，10 次刷新就是 30 行
2. 前端持有的 id 在下次刷新后失效——旧行还在但不是最新内容
3. ID 方案的稳定性依赖 id 不变

### 1.5 OBS 未启用时不写 DB

builder.py:123-124，OBS 未启用时 `_write_to_obs` 直接 return，不写 DB。baseline 文件只有本地文件，没有 DB 记录 → 前端拿不到 id → ID 方案无法覆盖 baseline。

## 二、方案设计

### 2.1 核心思路

前端传 DB 主键 id，后端通过 id 查 DB 拿到 obs_key + file_path，自行决定走 OBS 还是本地文件。前端不再关心 obs_key 还是 file path。

### 2.2 接口改造

#### 2.2.1 GET /api/knowledge/content（读取详情）

新增 `id` 参数，不保留 `file` 参数（改完后 baseline 始终有 DB 记录，不需要 file 兜底）：

- id 非空 → 查 DB 拿 repo_key + obs_key + file_path（一次查询）→ 查不到返回 404 → 校验 repo_key 归属（不通过返回 403）→ obs_key 非空读 OBS，miss 走 _read_ws_file(file_path)
- id 为空或不传 → 400

废弃 `obs_key` 参数和 `file` 参数，前端不传后端不接收。用户刷新页面加载新前端即可。

#### 2.2.2 列表查询加 id 字段

所有列表查询 SELECT 加 id，返回数据加 id 字段：

| 函数 | 位置 | 改动 |
|---|---|---|
| baseline 查询 | routes/knowledge.py:364-378 | SELECT 加 id，返回加 id |
| _project_knowledge_entries | routes/knowledge.py:252-276 | SELECT 加 id，返回加 id |
| _agent_knowledge_entries | routes/knowledge.py:279-343 | SELECT 加 id，返回加 id |
| _member_docs | routes/knowledge.py:161-241 | SELECT 已有 id，返回加 id |

#### 2.2.3 前端改造

`loadKnowledgeContent` 改成传 id，所有 onclick 调用点改传 id：

- baseline：`f.id`（回退 `f.name`）
- knowledge 条目：`k.id`
- member docs：`doc.id`
- agent 条目：`k.id`

### 2.3 权限校验

#### 2.3.1 校验策略

每个接口一次校验，不需要缓存：

| 接口 | 入参 | 校验方式 |
|---|---|---|
| GET /knowledge (list) | repo | 前端传 repo_key → 校验用户关联该 repo_key |
| GET /knowledge/content | id | 用 id 查 DB 拿 repo_key → 校验用户关联该 repo_key |
| POST /knowledge (upload) | repo | 前端传 repo_key → 校验用户关联该 repo_key |
| DELETE /knowledge | id | 用 id 查 DB 拿 repo_key → 校验用户关联该 repo_key + 本人上传 |

#### 2.3.2 权限查询函数

在 user/db.py 新增轻量查询函数，单条 SQL：

```python
def is_user_repo_joined(user_id: int, repo_key: str) -> bool:
    """校验用户是否已加入指定仓库（user_repos.is_join=1）。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT 1 FROM user_repos WHERE user_id=? AND repo_key=? AND is_join=1 LIMIT 1",
            (user_id, repo_key)).fetchone()
        return row is not None
```

#### 2.3.3 校验调用

在 routes/knowledge.py 抽一个内部函数：

```python
def _check_repo_access(user: dict, repo_key: str):
    from src.user.db import is_user_repo_joined
    if not is_user_repo_joined(user["id"], repo_key):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该仓库"})
```

- list/upload：直接用前端传的 repo → to_repo_key → _check_repo_access
- content/delete：用 id 查 DB 拿 repo_key → _check_repo_access
- delete 额外校验：仅可删除本人上传的文档（已有逻辑，保留）

### 2.4 OBS 未启用也写入 DB

#### 2.4.1 问题

builder.py:123-124，OBS 未启用时 `_write_to_obs` 直接 return，不写 DB。baseline 文件只有本地文件，没有 DB 记录 → 前端拿不到 id → ID 方案无法覆盖 baseline。

#### 2.4.2 改法

把 DB 写入从 OBS 分支里提出来，无论 OBS 是否启用都写 DB，obs_key 为空表示本地模式。

### 2.5 baseline 写入改先查后更新 + 分布式锁

#### 2.5.1 问题

INSERT OR REPLACE 无 UNIQUE 约束导致每次 INSERT 新行，id 变化，DB 累积重复行。

#### 2.5.2 改法

改为先查后更新（SELECT + INSERT/UPDATE），不加唯一索引，不改 agent_shared_knowledge 表结构。并发安全通过分布式锁保证。

锁的位置在 maybe_refresh_project_info 入口层（webhook 真实入口），包裹 should_refresh + build_project_info 完整流程。build_project_info 内部不再抢锁，_write_to_obs 内部不再抢锁。

锁 key 使用 per-repo 维度（baseline:{repo_key}），不是 per-file 维度。should_refresh + build 整个项目期间持有锁，结束后释放。

TTL 设置：锁持有时间 = should_refresh（含远程 commit HTTP 请求）+ build_project_info 全流程。builder.py 里 git worktree timeout 900s、codegraph init timeout 600s，极端情况 build 可能超过 300s。TTL 设为 900 秒覆盖上限。不引入看门狗续期（增加复杂度），锁过期后其他实例可抢锁，但结果幂等（OBS 同 key 覆盖 + DB 先查后更新），不影响正确性。

_write_to_obs 内部不再承担锁职责，只负责 DB/OBS 持久化。保留原有 try/except 降级逻辑：DB/OBS 写入失败仅 log warning，不阻断 build 流程，本地文件保留。下次 maybe_refresh_project_info 触发时 DB 缺失校验会强制 rebuild 补齐记录。

should_refresh 保持纯粹（只做本地文件 + 远程 commit 判断），DB baseline 记录存在性校验放在 maybe_refresh_project_info 调用 should_refresh 之后执行，不污染 should_refresh 签名。

#### 2.5.3 并发场景分析

生产环境使用 MySQL（configs/db-backend.json backend=mysql），多实例部署 + webhook 高频触发可能导致并发写 baseline：

- 同进程内：builder.py:339-359 `_building` set + threading.Lock 防同 repo 并发 build
- 跨实例：`_building` 是进程内 set，多实例各自独立，互不感知
- db_lock（threading.RLock）只保护单进程内多线程串行，跨进程无效
- 先查后更新在 db_lock 块内做 SELECT + INSERT/UPDATE，进程内原子，但跨进程有竞态窗口

因此需要跨实例分布式锁。

### 2.6 分布式锁表设计

#### 2.6.1 表结构

```sql
CREATE TABLE IF NOT EXISTS distributed_locks (
    lock_key    VARCHAR(128) NOT NULL COMMENT '锁唯一key',
    holder      VARCHAR(64)  NOT NULL COMMENT '锁持有者instanceId',
    locked_at   BIGINT       NOT NULL COMMENT '获取锁时间戳(秒)',
    expires_at  BIGINT       NOT NULL COMMENT '锁过期时间戳(秒)',
    update_at   BIGINT       NOT NULL DEFAULT 0 COMMENT '最后更新时间，用于排查',
    PRIMARY KEY (lock_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MySQL数据库分布式锁表';
```

设计要点：
- `lock_key` 主键唯一，天然保证同一把锁只能存在一行
- `holder` 持有者标识，只有持有者才能释放锁
- `expires_at` 过期时间戳，解决实例宕机死锁
- `update_at` 最后更新时间，排查问题用
- 显式指定 `ENGINE=InnoDB`（依赖行锁，MyISAM 不支持）
- 秒级时间戳，baseline 写入是秒级操作，精度够用

#### 2.6.2 抢锁（原子）

用 INSERT ... ON DUPLICATE KEY UPDATE，MySQL 主键约束 + 行锁保证原子性。过期判断放 SQL 层用 UNIX_TIMESTAMP()，不用应用本机时间（避免时钟漂移）。

**SQL 方言处理（方案 A：后端分支）**

MySQL 后端绕过 translate_sql()，直接执行原生 MySQL SQL；SQLite 后端独立实现抢锁逻辑（INSERT OR ABORT 捕获唯一冲突 + 条件 UPDATE）：

MySQL 抢锁 SQL：

```sql
INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at)
VALUES (*** UNIX_TIMESTAMP(), UNIX_TIMESTAMP()+60, UNIX_TIMESTAMP())
ON DUPLICATE KEY UPDATE
    holder = IF(expires_at < UNIX_TIMESTAMP(), VALUES(holder), holder),
    locked_at = IF(expires_at < UNIX_TIMESTAMP(), VALUES(locked_at), locked_at),
    expires_at = IF(expires_at < UNIX_TIMESTAMP(), VALUES(expires_at), expires_at),
    update_at = IF(expires_at < UNIX_TIMESTAMP(), VALUES(update_at), update_at)
```

SQLite 抢锁逻辑（分两步，db_lock 块内原子）：

```sql
-- 1. 尝试插入
INSERT OR ABORT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at)
VALUES (*** strftime('%s','now'), strftime('%s','now')+60, strftime('%s','now'))
-- 2. 主键冲突 → 查现有行判断是否过期
SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?
-- 3. 已过期 → 覆盖
UPDATE distributed_locks SET holder=*** expires_at=? WHERE lock_key=? AND expires_at < strftime('%s','now')
-- 4. 回查确认
SELECT holder FROM distributed_locks WHERE lock_key=?
```

逻辑（两种后端一致）：
- 锁不存在 → INSERT 成功，拿到锁
- 锁存在且未过期 → 不改值，抢锁失败
- 锁存在但已过期 → IF 条件成立，覆盖为新的 holder，拿到锁

抢锁后回查 holder 确认是否抢到（不靠 affected_rows 判断，避免返回值坑）：

```sql
SELECT holder FROM distributed_locks WHERE lock_key=?
```

#### 2.6.3 释放锁

```sql
DELETE FROM distributed_locks WHERE lock_key=? AND holder=?
```

只删自己的锁，不会误删别人的。

#### 2.6.4 过期锁清理

抢锁时不额外清理（抢锁逻辑已处理过期抢占）。单独创建后台清理任务防止表膨胀，5 分钟一次：

```sql
DELETE FROM distributed_locks WHERE expires_at < UNIX_TIMESTAMP() AND holder != ?
```

只删除已过期且不属于当前实例的锁，避免误删正在续期的锁。清理任务是防表膨胀兜底，业务抢锁不依赖定时清理。

#### 2.6.5 DDL SQLite 兼容

db_setup.py 建表时判断数据库后端，SQLite 模式下裁剪 MySQL 专属子句：

```python
raw_ddl = """CREATE TABLE IF NOT EXISTS distributed_locks (...) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='xxx';"""
if db_backend == "sqlite":
    raw_ddl = re.sub(r"\s+ENGINE\s*=\s*\w+", "", raw_ddl, flags=re.IGNORECASE)
    raw_ddl = re.sub(r"\s+DEFAULT\s+CHARSET\s*=\s*\w+", "", raw_ddl, flags=re.IGNORECASE)
cursor.execute(raw_ddl)
```

#### 2.6.6 封装函数

新建 src/lock.py：

```python
"""分布式锁——基于 MySQL distributed_locks 表。

跨实例互斥，per-key 粒度，TTL 超时自动释放。
MySQL 后端绕过 translate_sql() 直接执行原生 SQL。
SQLite 后端独立实现（INSERT OR ABORT + 条件 UPDATE）。
"""
import socket, secrets, time
from src.config import log
from src.session_manager import db, db_lock, _is_mysql_backend

_holder = f"{socket.gethostname()}-{secrets.token_hex(4)}"


def acquire_lock(lock_key: str, ttl: int = 60) -> bool:
    """抢分布式锁，成功返回 True。"""
    if not _is_mysql_backend():
        # SQLite：INSERT OR ABORT + 条件 UPDATE（db_lock 块内原子）
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT OR ABORT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at) "
                    "VALUES (*** strftime('%s','now'), strftime('%s','now')+?, strftime('%s','now'))",
                    (lock_key, _holder, ttl))
                db.commit()
                return True  # INSERT 成功 = 拿到锁
            except Exception:
                pass  # 主键冲突 → 查现有行
            row = c.execute(
                "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=*** (lock_key,)).fetchone()
            if not row:
                return False
            # 已过期 → 覆盖
            if int(row[1] or 0) < int(time.time()):
                c.execute(
                    "UPDATE distributed_locks SET holder=*** expires_at=strftime('%s','now')+? "
                    "WHERE lock_key=*** AND expires_at < strftime('%s','now')",
                    (_holder, ttl, lock_key))
                db.commit()
                row2 = c.execute(
                    "SELECT holder FROM distributed_locks WHERE lock_key=*** (lock_key,)).fetchone()
                return row2 and row2[0] == _holder
            return False  # 未过期，抢锁失败

    # MySQL：绕过 translate_sql，直接执行原生 SQL
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, update_at) "
            "VALUES (*** UNIX_TIMESTAMP(), UNIX_TIMESTAMP()+?, UNIX_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            "  holder=IF(expires_at < UNIX_TIMESTAMP(), VALUES(holder), holder), "
            "  locked_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(locked_at), locked_at), "
            "  expires_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(expires_at), expires_at), "
            "  update_at=IF(expires_at < UNIX_TIMESTAMP(), VALUES(update_at), update_at)",
            (lock_key, _holder, ttl))
        db.commit()
        row = c.execute(
            "SELECT holder FROM distributed_locks WHERE lock_key=*** (lock_key,)).fetchone()
        return row and row[0] == _holder


def release_lock(lock_key: str):
    """释放锁。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM distributed_locks WHERE lock_key=*** AND holder=*** (lock_key, _holder))
        db.commit()


def cleanup_expired_locks():
    """清理过期锁（后台定时调用，5 分钟一次）。只删不属于当前实例的过期锁。"""
    with db_lock:
        c = db.cursor()
        if _is_mysql_backend():
            c.execute("DELETE FROM distributed_locks WHERE expires_at < UNIX_TIMESTAMP() AND holder != ?", (_holder,))
        else:
            c.execute("DELETE FROM distributed_locks WHERE expires_at < strftime('%s','now') AND holder != ?", (_holder,))
        db.commit()
```

#### 2.6.7 baseline 写入使用

锁上移到 maybe_refresh_project_info 入口层（webhook 真实入口），包裹 should_refresh + build_project_info 完整流程。build_project_info 和 _write_to_obs 内部不再抢锁：

```python
from src.lock import acquire_lock, release_lock

def maybe_refresh_project_info(repo_key, token=""):
    """webhook 触发的后台刷新。分布式锁 + 内存 _building 双重防重。"""
    if not repo_key:
        return
    with _lock_guard:
        if repo_key in _building:
            return
        _building.add(repo_key)

    # 先抢分布式锁（per-repo，TTL 900 秒覆盖 should_refresh HTTP + build 全流程）
    lock_key=f"base...ey}"
    acquired = False
    try:
        acquired = acquire_lock(lock_key, ttl=900)
        if not acquired:
            log.info(f"baseline lock contention: {repo_key}, skipped")
            return

        # 拿到锁后才执行 should_refresh（含远程 commit HTTP 请求）
        local_need = should_refresh(repo_key)
        # DB baseline 记录存在性校验（不污染 should_refresh，放在调用方）
        db_missing = not _baseline_exists(repo_key)
        if not (local_need or db_missing):
            return

        log.info(f"project-info refresh triggered for {repo_key}")
        build_project_info(repo_key, token)
    except Exception as e:
        log.warning(f"project-info refresh failed {repo_key}: {e}")
    finally:
        if acquired:
            release_lock(lock_key)
        with _lock_guard:
            _building.discard(repo_key)


def _baseline_exists(repo_key) -> bool:
    """检查 DB 是否存在该 repo 的 baseline 记录。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT 1 FROM agent_shared_knowledge WHERE scope='baseline' AND repo_key=? LIMIT 1",
                (repo_key,)).fetchone()
        return row is not None
    except Exception:
        return False


def build_project_info(repo_key, token=""):
    # 不再抢锁，锁在 maybe_refresh_project_info 层
    ...
    _write_meta(repo_key, meta)
    _write_file(repo_key, _GRAPH_FILE, graph_content)
    _write_file(repo_key, _CAP_FILE, cap_content)
    ...


def _write_to_obs(repo_key, name, content):
    """写 OBS + 落库 baseline 记录。不再抢锁，锁在 maybe_refresh_project_info 层。
    保留原有 try/except 降级：DB/OBS 写入失败仅 log warning，不阻断 build。"""
    try:
        file_path = os.path.relpath(os.path.join(_pi_dir(repo_key), name), WORKSPACE_DIR)
        obs_key = ""
        content_hash = ""

        storage = get_storage()
        if storage:
            storage_key = f"project-info/{repo_key}/{name}"
            storage.save_bytes(storage_key, content.encode("utf-8"))
            obs_key = storage_key
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:64]

        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT id FROM agent_shared_knowledge WHERE scope='baseline' AND repo_key=*** AND file_path=?",
                (repo_key, file_path)).fetchone()
            if row:
                c.execute(
                    "UPDATE agent_shared_knowledge SET obs_key=*** content_hash=?, status='accepted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (obs_key, content_hash, row[0]))
            else:
                c.execute(
                    "INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type, issue_num, file_path, status, obs_key, content_hash, created_by) "
                    "VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'vod')",
                    (repo_key, file_path, obs_key, content_hash))
            db.commit()
    except Exception as e:
        log.warning(f"project-info OBS/DB write failed {repo_key}/{name}: {e} — local file unaffected, next refresh will retry")
```

#### 2.6.8 GET_LOCK 不采用的对比

项目 db_setup.py:752 迁移逻辑用 GET_LOCK 跨实例互斥，baseline 不采用，原因：
- GET_LOCK 放在 db_lock 块内，等待锁期间 db_lock 被持有，整个进程其他 DB 操作阻塞
- GET_LOCK 依赖 MySQL 连接保持，连接池切换/断线后锁释放行为不明确
- 分布式锁表方案更可控：TTL 超时、holder 校验、过期清理，不依赖连接

## 三、改动清单

共 6 个文件：

| # | 文件 | 改动点 |
|---|---|---|
| 1 | src/api/db_setup.py | 新增 distributed_locks 表 DDL（SQLite 裁剪 ENGINE/CHARSET） |
| 2 | src/lock.py（新建） | 分布式锁封装：acquire_lock（MySQL/SQLite 双分支）/ release_lock / cleanup_expired_locks + 后台清理任务 |
| 3 | src/project_info/builder.py | maybe_refresh_project_info 入口抢锁（TTL 900s）+ should_refresh 后 DB 校验 + _write_to_obs 去掉内部锁保留 try/except 降级 + OBS 未启用也写 DB + 先查后更新 |
| 4 | src/user/db.py | 新增 `is_user_repo_joined` 轻量权限查询函数 |
| 5 | src/api/routes/knowledge.py | 列表查询 4 个函数加 id 字段 + read_knowledge_content 改 id 参数（一次查询拿 repo_key+obs_key+file_path）+ 四个接口加 _check_repo_access |
| 6 | src/api/static/js/pages/knowledge.js | loadKnowledgeContent 改传 id + 所有 onclick 调用点改传 id |

## 四、不受影响

| 点 | 原因 |
|---|---|
| _read_file / _read_ws_file / _read_entry_content | 底层读取函数，上层换调用方式即可 |
| write_knowledge 写入逻辑 | 保持现状（append + INSERT 审计行），不改 |
| agent 知识沉淀逻辑 | write_knowledge 不动，看板展示不动，原逻辑保持 |
| agent 知识注入逻辑 | read_knowledge 按 scope 查 DB ORDER BY id DESC LIMIT 1，只读最新一行 obs_key → 读一份全文。DB 多行不影响注入，不会传多条重复数据给 agent |
| agent_shared_knowledge 表结构 | 不加 UNIQUE 索引，不加字段 |
| knowledge_sync_loop | 扫 obs_key 为空的行，跟 id 无关 |
| generate_file_urls | 生成临时 URL，不走 /content 接口 |
| knowledge_files / member_files / agent_files | 交付快照用，不走 /content 接口 |
| /context 接口 | 直接调 prompt_builder，不走 /content |
| read_knowledge（注入上下文） | 直接读文件/OBS 全文，不查 DB status，不受影响 |

## 五、风险与注意点

1. **安全**：id 是连续整数，用户可猜测。content/delete 接口用 id 查 DB 后必须校验 repo_key 归属当前用户，不能只按 id 查不带 repo_key。
2. **baseline 重复行清理**：已有重复行不影响功能（旧行 obs_key 相同，读到的 OBS 内容是最新的），建议实施后清理一次：`DELETE FROM agent_shared_knowledge WHERE scope='baseline' AND id NOT IN (SELECT MAX(id) FROM agent_shared_knowledge WHERE scope='baseline' GROUP BY repo_key, file_path)`。清空后用户重新触发 webhook（push commit / 开 issue）即可重建。
3. **OBS 未启用兼容**：baseline 在 OBS 未启用时 obs_key 为空，后端读内容时走 _read_ws_file(file_path) 读本地文件。
4. **前端旧缓存**：不兼容旧前端，用户刷新页面加载新前端即可。
5. **分布式锁双后端兼容**：MySQL 绕过 translate_sql 直接执行原生 SQL；SQLite 用 INSERT OR ABORT + 条件 UPDATE 独立实现。cleanup_expired_locks 同样双分支（UNIX_TIMESTAMP / strftime）。两种后端均需测试覆盖。
6. **锁位置**：锁在 maybe_refresh_project_info 入口层，包裹 should_refresh + build_project_info 完整流程，TTL 900 秒。build_project_info 和 _write_to_obs 内部不加锁。抢锁失败直接 return，不执行 should_refresh，不产生无效 HTTP 请求。
7. **should_refresh DB 兜底**：DB baseline 记录存在性校验放在 maybe_refresh_project_info 调用 should_refresh 之后（方案 B），不污染 should_refresh 签名。DB 无记录时强制 rebuild 补齐。
8. **锁表初始化**：distributed_locks 表在 db_setup.py 服务启动阶段强制创建，建表失败阻断服务启动。acquire_lock 不处理表不存在场景，表缺失属于初始化故障。
9. **过期锁清理**：后台单独清理任务 5 分钟一次，DELETE 带 holder 过滤，不误删当前实例持有的锁。
10. **_write_to_obs 降级保留**：保留原有 try/except，DB/OBS 写入失败仅 log warning 不阻断 build，本地文件保留。下次 refresh 时 DB 缺失校验触发 rebuild 补齐。
11. **content 接口 404**：id 非空但 DB 查不到记录返回 404，repo_key 权限不通过返回 403。
12. **代码路径审计**：PR 阶段通读 maybe_refresh_project_info → build_project_info 全部 return 路径，确认所有业务 return 都是函数返回不会绕过外层 finally 锁释放，子函数内禁止 sys.exit/os._exit。
