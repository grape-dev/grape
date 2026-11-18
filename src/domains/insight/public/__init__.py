"""insight 域对外 lib（进程内 import，使用 insight 自己的 DSN）。

与 mgmt/runtime public 的约定一致（方案 §3.5）：
- 只返回 JSON 友好类型；
- 写函数接受调用方给的幂等键/唯一键，SQL 固定、参数化；
- `available()` 要求 insight 自有 DSN（`GRAPE_DSN_INSIGHT` + `GRAPE_DOMAIN_LIB=1`），
  否则调用方必须走原路径（禁止静默串库）。

本模块当前只承载 W3 派生写（指标/日志落库）：`api_latency_log`、`grape_t_merge_gate_log`。
"""
from __future__ import annotations

from src.kernel.config import log

from contextlib import contextmanager
from typing import Any

from src.kernel import db_registry


_REPO_CRED_QUERIES: dict[str, str] = {
    "repo_cred_rows": (
        "SELECT id, repo_key, cred_type, value, updated_at FROM grape_t_repo_credentials"),
    "repo_cred_rows_by_key": (
        "SELECT id, repo_key, cred_type, value, updated_at FROM grape_t_repo_credentials "
        "WHERE repo_key=?"),
    "repo_cred_value_id": (
        "SELECT value, id FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type=?"),
    "repo_cred_value": (
        "SELECT value FROM grape_t_repo_credentials WHERE repo_key=? AND cred_type=?"),
    "repo_cred_repos_by_value": (
        # 前缀相等而非 LIKE：repo_key 前缀里可能带下划线，_ 是 LIKE 单字符通配符
        "SELECT repo_key FROM grape_t_repo_credentials WHERE cred_type=? "
        "AND (value=? OR value=?) AND substr(repo_key, 1, ?) = ? AND repo_key!=?"),
    "repo_cred_update": (
        "UPDATE grape_t_repo_credentials SET value=?, updated_at=? WHERE repo_key=? AND cred_type=?"),
    "repo_cred_insert": (
        "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value, updated_at) "
        "VALUES (?,?,?,?,?)"),
    "repo_cred_clear": (
        "UPDATE grape_t_repo_credentials SET value='', updated_at=? "
        "WHERE repo_key=? AND cred_type=?"),
    "repo_cred_nonempty_keys": (
        "SELECT repo_key FROM grape_t_repo_credentials WHERE cred_type=? "
        "AND value IS NOT NULL AND value!=''"),
    "repo_bot_credential_pairs": (
        "SELECT repo_key, value FROM grape_t_repo_credentials WHERE cred_type='bot'"),
    "repo_cred_nonempty_keys_all": (
        "SELECT repo_key FROM grape_t_repo_credentials WHERE value IS NOT NULL AND value!=''"),
    # 存量回填面（D90-N3/N4）：列序与原回填 SQL 逐字一致；`!= ''` 在 sqlite/MySQL 同义，
    # 且 NULL 行两边都不命中（NULL 比较结果为 NULL，非真）——与改造前分流一致。
    "repo_cred_nonempty_rows": (
        "SELECT repo_key, cred_type, value FROM grape_t_repo_credentials WHERE value != ''"),
    "repo_cred_value_only_update": (
        "UPDATE grape_t_repo_credentials SET value=? WHERE repo_key=? AND cred_type=?"),
    "repo_cred_empty_id_rows": (
        "SELECT repo_key, cred_type FROM grape_t_repo_credentials WHERE id IS NULL OR id=''"),
}

# 指标留存（`obs_metric_rollup` 归 insight）：管理台 `scope=all` 需要枚举已出现过的维度键。
_ROLLUP_QUERIES: dict[str, str] = {
    "rollup_dim_keys": "SELECT DISTINCT dim_key FROM obs_metric_rollup WHERE dim_key != ?",
}


def available() -> bool:
    """insight lib 是否可用：必须配置 insight 自有 DSN。"""
    return db_registry.uses_own_dsn("insight")


def record_api_latency(request_id: str, method: str, path: str, status: int,
                       duration_ms: float, repo_key: str = "", user_id: str = "",
                       source: str = "", created_at: str = "") -> int:
    """API 延迟指标落库（W3）：纯派生数据，重放由调用方幂等键保证。"""
    with db_registry.cursor("insight") as c:
        c.execute("INSERT INTO api_latency_log "
                  "(request_id, method, path, status, duration_ms, repo_key, user_id, "
                  "source, created_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(NULLIF(%s, ''), NOW()))",
                  (request_id, method, path, int(status), float(duration_ms), repo_key,
                   user_id, source, created_at or ""))
        return int(getattr(c, "lastrowid", 0) or 0)


def record_merge_gate_log(repo_key: str, pr_number: int, phase: str, decision: str,
                          hits_json: str = "", created_at: str = "") -> str:
    """合并门禁日志（W3）：流水，重放由调用方幂等键保证。

    `id` 为 varchar(191) 主键（非自增），由本函数生成 uuid7 并返回。
    """
    from src.kernel.id_gen import uuid7_str

    new_id = uuid7_str()
    with db_registry.cursor("insight") as c:
        c.execute("INSERT INTO grape_t_merge_gate_log "
                  "(id, repo_key, pr_number, phase, decision, hits_json, created_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(NULLIF(%s, ''), NOW()))",
                  (new_id, repo_key, int(pr_number), phase, decision, hits_json,
                   created_at or ""))
        return new_id


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("insight") as c:
            c.execute("SELECT COUNT(*) FROM api_latency_log")
            return {"ok": True, "api_latency_rows": int(c.fetchone()[0]),
                    "dsn_configured": bool(db_registry.dsn_of("insight"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160],
                "dsn_configured": bool(db_registry.dsn_of("insight"))}


__all__ = ["available", "record_api_latency", "record_merge_gate_log", "health"]

# ---------- 任意拓扑原语（自有 DSN 走 db_registry；单库/测试拓扑回落进程主库）----------
#
# 与 mgmt/runtime public 的 `*_any` 组同口径：SQL 一律可移植方言（`?` 占位符 /
# `datetime('now')`），两条连接都能执行，因此调用方不必在外域文件里留本地兜底 SQL。


@contextmanager
def _any_cursor(write: bool = False):
    """insight 表游标：自有 DSN 优先（autocommit），否则回落进程主库（持有 db_lock）。"""
    if db_registry.uses_own_dsn("insight"):
        with db_registry.cursor("insight") as c:
            yield c
        return
    from src.kernel.session_manager import db as _pdb, db_lock as _plock

    with _plock:
        c = _pdb.cursor()
        yield c
        if write:
            _pdb.commit()


@contextmanager
def _any_tx():
    """多语句组合写：自有 DSN → 独立非 autocommit 连接（真事务）；单库 → 进程库 + db_lock。"""
    if db_registry.uses_own_dsn("insight"):
        with db_registry.transaction("insight") as c:
            yield c
        return
    from src.kernel.session_manager import db as _pdb, db_lock as _plock

    with _plock:
        c = _pdb.cursor()
        try:
            yield c
            _pdb.commit()
        except Exception:
            try:
                _pdb.rollback()
            except Exception as exc:  # noqa: BLE001 - 回滚失败不掩盖原异常
                log.debug(f"insight public rollback skipped: {exc}")
            raise


def _any_sql(name: str) -> str:
    sql = _REPO_CRED_QUERIES.get(name)
    if sql is None:
        sql = _ROLLUP_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown portable query: {name}")
    return sql


def read_one_any(name: str, params: tuple = ()) -> Any | None:
    with _any_cursor() as c:
        c.execute(_any_sql(name), params)
        return c.fetchone()


def read_all_any(name: str, params: tuple = ()) -> list[Any]:
    with _any_cursor() as c:
        c.execute(_any_sql(name), params)
        return list(c.fetchall() or [])


def exec_any(name: str, params: tuple = ()) -> int:
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql(name), params)
        return int(getattr(cur, "rowcount", 0) or 0)


# ---------- 仓库凭据（grape_t_repo_credentials 归 insight：唯一权威读写口）----------

def repo_credential_value(repo_key: str, cred_type: str) -> str:
    """读单条凭据值（未解密，调用方自行 decrypt）；无行返回空串。"""
    row = read_one_any("repo_cred_value", (repo_key, cred_type))
    return (row[0] or "") if row else ""


def repo_credential_meta(repo_key: str, cred_type: str) -> tuple | None:
    """读 (value, 行 id) 一次查询（value 与审计锚点同事务一致）；无行返回 None。"""
    return read_one_any("repo_cred_value_id", (repo_key, cred_type))


def upsert_repo_credential_value(repo_key: str, cred_type: str, value: str,
                                 updated_at: str) -> dict:
    """幂等 upsert：UPDATE → rowcount==0 → INSERT → 并发冲突再 UPDATE（同一事务）。

    返回 {"action": updated|inserted|updated_after_conflict, "id": 行 id 或 0}；
    只写不关心结果的调用方可忽略返回值。
    """
    from src.kernel.id_gen import uuid7_str

    with _any_tx() as c:
        cur = c.execute(_any_sql("repo_cred_update"), (value, updated_at, repo_key, cred_type))
        if int(getattr(cur, "rowcount", 0) or 0) > 0:
            return {"action": "updated", "id": 0}
        try:
            new_id = uuid7_str()
            c.execute(_any_sql("repo_cred_insert"),
                      (new_id, repo_key, cred_type, value, updated_at))
            return {"action": "inserted", "id": new_id}
        except Exception:
            # 并发插入撞唯一约束 → retry UPDATE（与原实现同语义）
            c.execute(_any_sql("repo_cred_update"), (value, updated_at, repo_key, cred_type))
            return {"action": "updated_after_conflict", "id": 0}


def upsert_repo_credentials(repo_key: str, items: list[tuple[str, str]],
                            updated_at: str) -> list[dict]:
    """**多行同事务**幂等 upsert：一次 `_any_tx()` 内写多条 (cred_type, value)。

    与 `upsert_repo_credential_value` 共用同一套两步法语句（UPDATE → rowcount==0 →
    INSERT → 撞唯一约束再 UPDATE），差别只在事务边界：全局 bot token 换发要同时写
    `bot` 与 `webhook` 两行，拆成两次单条调用就会在两次提交之间留下「bot 已换、
    webhook 仍旧」的半写状态。返回列表与 items 同序（`{"action", "id"}`）。
    """
    from src.kernel.id_gen import uuid7_str

    out: list[dict] = []
    with _any_tx() as c:
        for cred_type, value in items:
            cur = c.execute(_any_sql("repo_cred_update"), (value, updated_at, repo_key, cred_type))
            if int(getattr(cur, "rowcount", 0) or 0) > 0:
                out.append({"action": "updated", "id": 0})
                continue
            try:
                new_id = uuid7_str()
                c.execute(_any_sql("repo_cred_insert"),
                          (new_id, repo_key, cred_type, value, updated_at))
                out.append({"action": "inserted", "id": new_id})
            except Exception:
                # 并发插入撞唯一约束 → retry UPDATE（与单条版同语义，同一事务内）
                c.execute(_any_sql("repo_cred_update"),
                          (value, updated_at, repo_key, cred_type))
                out.append({"action": "updated_after_conflict", "id": 0})
    return out


def list_repo_credential_rows(repo_key: str = "") -> list[tuple]:
    """仓库凭据行（id, repo_key, cred_type, value, updated_at；value 为密文，解密归调用方）。"""
    if repo_key:
        return [tuple(r) for r in read_all_any("repo_cred_rows_by_key", (repo_key,))]
    return [tuple(r) for r in read_all_any("repo_cred_rows")]


def clear_repo_credential_value(repo_key: str, updated_at: str, cred_type: str = "bot") -> int:
    """把指定类型凭据置空（默认 bot；返回影响行数）。"""
    return exec_any("repo_cred_clear", (updated_at, repo_key, cred_type))


def any_repo_credential(repo_keys: list[str]) -> bool:
    """任一仓库有非空凭据（has_bot 闸门 rc 感知）。"""
    keys = [k for k in (repo_keys or []) if k]
    if not keys:
        return False
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(f"SELECT 1 FROM grape_t_repo_credentials WHERE repo_key IN ({ph}) "
                  "AND value IS NOT NULL AND value != '' LIMIT 1", tuple(keys))
        return c.fetchone() is not None


def repo_keys_sharing_value(cred_type: str, values: tuple, prefix: str, exclude_repo: str) -> list[str]:
    """同一凭据值被哪些仓库共用（凭据泄漏排查用）。

    `prefix` 是**前缀字面量**（如 `gitcode:`），由本域按 substr 前缀相等过滤——
    不接受 `%` 通配：repo_key 前缀可带下划线，LIKE 会把其它平台静默算进来。
    """
    return [r[0] for r in read_all_any(
        "repo_cred_repos_by_value",
        (cred_type, values[0], values[1], len(prefix or ""), prefix or "", exclude_repo))]


def repo_bot_credential_pairs() -> list[tuple]:
    """全部 bot 凭据 (repo_key, 密文 value) 对（config_store 的跨域 JOIN 拆分后需要全量行）。"""
    return [tuple(r) for r in read_all_any("repo_bot_credential_pairs")]


def list_repo_keys_with_nonempty_credential(cred_type: str) -> list[str]:
    """有非空凭据的仓库 repo_key 列表（**保序**，不去重）。

    与 `repo_keys_with_nonempty_credential` 同一份具名查询，差别只在返回形态：
    该集合版给「本域侧 NOT EXISTS 等价过滤」用（顺序无关），此列表版给
    「逐个仓库巡检」的调用方用——原语句没有 ORDER BY，`fetchall()` 的行序即表序，
    换成 set 会打散巡检分批顺序，因此单独留一个保序入口，而不是让调用方自己排序。
    """
    rows = read_all_any("repo_cred_nonempty_keys", (cred_type,))
    return [r[0] for r in rows if r[0]]


def repo_keys_with_nonempty_credential(cred_type: str = "") -> set[str]:
    """有非空凭据的仓库集合（cred_type 为空 → 不限类型；供本域侧 NOT EXISTS 等价过滤）。"""
    if cred_type:
        rows = read_all_any("repo_cred_nonempty_keys", (cred_type,))
    else:
        rows = read_all_any("repo_cred_nonempty_keys_all")
    return {r[0] for r in rows if r[0]}


# ---------- 存量回填面（D90-N3 加密回填 / D99 空 id 断言）----------
#
# 这两步原在 mgmt 的 `user/credentials_backfill_n1.py` 里持 insight 游标直发 SQL——
# 表归 insight，SQL 只该留本域一份。下面三个入口把「读什么 / 改哪列」收回本域：
# 回填只写 `value` 列（**不动 updated_at**，与原 SQL 逐字一致），批量写在同一事务内
# 全成或全回滚；缺行不插入（回填不新增凭据行，与原 UPDATE 语义一致）。


def list_repo_credentials_with_nonempty_value() -> list[tuple]:
    """全部非空凭据行 `(repo_key, cred_type, value)`；value 未解密，判定归调用方。

    **保序**（无 ORDER BY，行序即表序）：加密回填逐行改写，行序不影响结果，但保持
    与改造前 `fetchall()` 同序，日志里的 `rows[:3]` 抽样可对齐。
    """
    return [tuple(r) for r in read_all_any("repo_cred_nonempty_rows")]


def update_repo_credential_values(pairs: list[tuple]) -> int:
    """批量改写凭据值：`[(repo_key, cred_type, value), ...]`，**同一事务**。

    复合键 `(repo_key, cred_type)` 定位；只写 value 列。任一条失败 → 整批回滚
    （调用方按「回填是幂等整批重放」的语义处理，不会留下半批密文半批明文）。
    返回提交条数；空列表零写、不取连接。
    """
    items = [tuple(p) for p in (pairs or [])]
    if not items:
        return 0
    with _any_tx() as c:
        for repo_key, cred_type, value in items:
            c.execute(_any_sql("repo_cred_value_only_update"), (value, repo_key, cred_type))
    return len(items)


def list_repo_credentials_without_id() -> list[tuple]:
    """`id` 为 NULL/空串的凭据行 `(repo_key, cred_type)`（D99 主键化后应恒空）。

    只读断言口：调用方据此告警「旧库未走新 DDL」，本域不做任何修补。
    """
    return [tuple(r) for r in read_all_any("repo_cred_empty_id_rows")]


# ---------- 指标留存维度（obs_metric_rollup 归 insight）----------

def rollup_dim_keys(exclude: str = "global") -> list[str]:
    """指标留存里出现过的维度键（默认排除平台汇总维度 `global`）。

    对应原管理台 `_all_rollup_dims()` 的平台视角：`scope=all` 逐维展开，
    `global` 维度由调用方单独按全局口径给，不重复枚举。
    口径照抄原 SQL：`dim_key != exclude`，空串维度（未标注维度）仍返回。
    """
    return [r[0] for r in read_all_any("rollup_dim_keys", (exclude,))]


__all__ += ["list_repo_credential_rows", "aggregate_llm_usage", "read_one_any", "read_all_any", "exec_any",
            "rollup_dim_keys", "record_api_latency_any",
            "repo_credential_value", "repo_credential_meta", "upsert_repo_credential_value",
            "upsert_repo_credentials",
            "clear_repo_credential_value", "any_repo_credential", "repo_keys_sharing_value",
            "repo_keys_with_nonempty_credential",
            "list_repo_credentials_with_nonempty_value", "update_repo_credential_values",
            "list_repo_credentials_without_id", "list_usage_detail_window"]


# ---------- 观测表聚合（obs_llm_usage 归 insight）----------

def aggregate_llm_usage(engine_ids: list[int]) -> list[tuple]:
    """按引擎聚合模型用量：返回 [(engine_id, model_key, model_raw, cnt, last_used), ...]。

    仅聚合模型名字段，不取 base_url/api_key（Issue #865 安全约束）。
    """
    ids = [int(x) for x in (engine_ids or []) if x]
    if not ids:
        return []
    ph = ",".join(["?"] * len(ids))
    with _any_cursor() as c:
        c.execute(
            f"SELECT engine_id, model_key, model_raw, COUNT(*) AS cnt, MAX(created_at) AS last_used "
            f"FROM obs_llm_usage WHERE engine_id IN ({ph}) "
            f"GROUP BY engine_id, model_key, model_raw", tuple(ids))
        return list(c.fetchall() or [])


# ---------- LLM 用量明细窗口读（obs_llm_usage 归 insight）：token 三级归因 ----------
#
# 原实现在 insight 侧 tokens.py 里发一条跨库宽 SQL（obs_llm_usage 左连接 agent_tasks）。
# `agent_tasks` 归 runtime，三库独立后本进程库里没有该表（视图 0）→ 整条语句 1146，
# 被调用方 except 吞成「空窗口」。跨域 JOIN 的正解是拆开：
#   ① 本域明细窗口读（本函数）；② runtime public `task_issue_ids()` 批量取映射；
#   ③ 调用方在 Python 内连接补 issue_id（缺失即 LEFT JOIN 的 NULL 侧）。
#
# 列序与原宽 SQL 的 `u.*` 侧逐字一致（task_id..cost_usd）；`?` 占位符，双方言同一文本。

_USAGE_DETAIL_SQL = (
    "SELECT task_id, agent_type, engine_id, repo_key, run_kind, run_id, created_at, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
    "cost_usd FROM obs_llm_usage WHERE created_at >= ? AND created_at <= ?")


def list_usage_detail_window(start_lo: str, start_hi: str, repo_keys=None, engine_ids=None,
                             task_id: str = "", limit: int = 20000) -> list[tuple]:
    """窗口内 LLM 用量明细（不含 issue_id，见本节头注释）。

    过滤口径与原 `tokens._scope` 逐字一致：
    - `repo_keys` / `engine_ids` 两组谓词**OR** 连接并整体加括号；两组皆空 → 不过滤（L2）；
    - `engine_ids` 里的假值（0/空串）丢弃（原实现 `[int(x) for x in ids if x]` 同口径），
      `repo_keys` 保留调用方给出的全部候选（原实现只判 `if keys`，不逐项过滤）；
    - `task_id` 非空 → 追加等值过滤；`limit` 恒附加（原 SQL 末尾 `LIMIT ?`）。
    """
    conds: list[str] = []
    args: list = [start_lo, start_hi]
    for col, values, drop_falsy in (("repo_key", repo_keys, False),
                                    ("engine_id", engine_ids, True)):
        vals = [v for v in (values or []) if v is not None]
        if drop_falsy:
            vals = [v for v in vals if v]
        if not vals:
            continue
        marks = ",".join(["?"] * len(vals))
        conds.append(f"{col} IN ({marks})")
        args.extend(vals)
    sql = _USAGE_DETAIL_SQL
    if conds:
        sql += " AND (" + " OR ".join(conds) + ")"
    if task_id:
        sql += " AND task_id = ?"
        args.append(str(task_id))
    sql += " LIMIT ?"
    args.append(max(1, int(limit)))
    with _any_cursor() as c:
        c.execute(sql, tuple(args))
        return list(c.fetchall() or [])


# ---------- API 延迟流水（api_latency_log 归 insight）：任意拓扑薄写口 ----------
#
# 原实现在 api 的慢请求中间件里直发 `INSERT INTO api_latency_log ...` 到 api 进程主库，
# 外面裹 `except: pass`。三库拆分后 api 进程库没有该表（视图 0）→ 慢请求指标整体静默丢
# 失，且丢在 except 里看不出。表归 insight，写口只留本域。
#
# 列序与 source 字面量 `'backend'` 与改造前逐字一致（created_at 由列默认值
# `datetime('now')` 给，原 SQL 也不写这一列）；`?` 占位符，sqlite / MySQL 同一文本。

_LATENCY_INSERT_SQL = (
    "INSERT INTO api_latency_log (request_id, method, path, status, duration_ms, "
    "repo_key, user_id, source) VALUES (?,?,?,?,?,?,?,'backend')")


def record_api_latency_any(request_id: str, method: str, path: str, status,
                           duration_ms, repo_key: str = "", user_id: str = "") -> int:
    """后端慢请求延迟落库（任意拓扑，返回影响行数）。

    纯派生数据，不做幂等去重（与原实现一致：每次慢请求一行）；status/duration_ms
    原样透传，调用方传什么存什么（与改造前同一口径）。
    """
    with _any_cursor(write=True) as c:
        cur = c.execute(_LATENCY_INSERT_SQL,
                        (request_id, method, path, status, duration_ms,
                         repo_key or "", user_id or ""))
        return int(getattr(cur, "rowcount", 0) or 0)


def merge_gate_log_insert(row_id: str, repo_key: str, pr_number: int, phase: str,
                          decision: str, hits_json: str, created_at: str) -> None:
    """合并门禁决策落库（表归本域，api 侧只调本原语）。"""
    with _any_cursor(write=True) as c:
        c.execute(
            "INSERT INTO grape_t_merge_gate_log "
            "(id, repo_key, pr_number, phase, decision, hits_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, repo_key, int(pr_number or 0), phase, decision, hits_json, created_at))


# ---------- 观测埋点 / 用量账本 / 反馈脱敏（observability 子包归 insight）----------
#
# 跨域调用方原本直接 import `insight.observability.<模块>` 的内部实现（mgmt / runtime 共 13 个
# 文件、23 个调用点）。埋点口径与用量表都归本域，这些入口收回 public。
#
# 两条硬约束，改这里之前先读：
# 1. 转发体一律**函数内 import**：observability 各模块在加载期会反向 import runtime / mgmt 的
#    public（如 events → runtime 事件总线、master_usage → runtime.engine.usage_collect），
#    顶层 import 会立刻成环。
# 2. 转发**不改语义**：埋点函数本身已保证「绝不拖垮主链路」（内部 try/except），这里再包一层
#    只会把真实 bug 藏进第二层 except。参数与返回值一律原样透传。


def redact_issue_text(text: str | None, max_len: int = 2000) -> str:
    """Issue 正文脱敏（feedback 落库前打码，出转时再打码一次兜存量明文；两层皆幂等）。"""
    from src.domains.insight.observability.redact_issue_body import redact_issue_text as _impl
    return _impl(text, max_len)


def normalize_tokens(raw: dict) -> dict:
    """把各家引擎的用量字段归一成 input/output/cache_read/cache_write/reasoning。"""
    from src.domains.insight.observability.llm_usage import normalize_tokens as _impl
    return _impl(raw)


def pipeline_trace_id(repo_key: str, issue_id: str, task_id: str = "") -> str:
    """流水线级 trace id（同一 (仓库, issue) 恒等；无 issue 退回任务级 id）。"""
    from src.domains.insight.observability.trace import pipeline_trace_id as _impl
    return _impl(repo_key, issue_id, task_id)


def record_task_enqueued(task_id: str, source: str, runtime_id: int) -> None:
    """任务入队埋点。"""
    from src.domains.insight.observability import record_task_enqueued as _impl
    return _impl(task_id, source, runtime_id)


def record_task_dispatched(task_id: str, source: str, runtime_id: int,
                           queue_wait_seconds: float = -1) -> None:
    """任务被 claim / dispatched 埋点（含排队等待秒数，-1 表示未知）。"""
    from src.domains.insight.observability import record_task_dispatched as _impl
    return _impl(task_id, source, runtime_id, queue_wait_seconds)


def record_task_terminal(task_id: str, source: str, runtime_id: int, terminal_status: str,
                         run_seconds: float = -1, total_seconds: float = -1) -> None:
    """任务到达终态埋点。"""
    from src.domains.insight.observability import record_task_terminal as _impl
    return _impl(task_id, source, runtime_id, terminal_status, run_seconds, total_seconds)


def record_task_failed(task_id: str, source: str, runtime_id: int,
                       failure_reason: str = "") -> None:
    """任务失败埋点（与 `record_task_terminal` 分开：失败面额外记原因）。"""
    from src.domains.insight.observability import record_task_failed as _impl
    return _impl(task_id, source, runtime_id, failure_reason)


def record_llm_usage(provider: str, model: str, input_tokens: int, output_tokens: int,
                     cache_tokens: int = 0) -> None:
    """Prometheus 侧 LLM token 埋点（与用量台账同源）。"""
    from src.domains.insight.observability import record_llm_usage as _impl
    return _impl(provider, model, input_tokens, output_tokens, cache_tokens)


def record_usage(task_id: str, usages, source: str = "report", run_kind: str = "task",
                 run_id: str = "", attr: dict | None = None) -> int:
    """LLM 用量台账写入，返回实际写入/更新行数；异常在实现内吞掉，不上抛。"""
    from src.domains.insight.observability.llm_usage import record_usage as _impl
    return _impl(task_id, usages, source=source, run_kind=run_kind, run_id=run_id, attr=attr)


def task_model_summary(task_id: str, runtime_id: int = 0) -> dict:
    """任务执行模型的统一取值口径（用量优先、配置模型回落、皆空 unknown）。"""
    from src.domains.insight.observability.llm_usage import task_model_summary as _impl
    return _impl(task_id, runtime_id)


def master_usage_for_task(task_id: str, engine: str, cwd: str = "", session_id: str = "",
                          **kwargs) -> Any:
    """取「通常有 agent_tasks 行」的一次运行用量采集器（透传 master_usage.for_task）。

    返回的是进程内采集器对象（`MasterRun` 实例，`.start()/.finish()` 就地采集），
    不是数据行——跨进程跑引擎的调用方仍应走 GSB，不在这里。
    """
    from src.domains.insight.observability.master_usage import for_task as _impl
    return _impl(task_id, engine, cwd=cwd, session_id=session_id, **kwargs)


def master_run(*args, **kwargs) -> Any:
    """构造一次主控机内引擎运行的用量采集器（形状同 `master_usage.MasterRun`）。

    只做构造转发：调用方拿到的是真实实例，`attr` 归属口径与直接构造完全一致。
    """
    from src.domains.insight.observability.master_usage import MasterRun as _impl
    return _impl(*args, **kwargs)


def new_run_id(prefix: str) -> str:
    """给「一次运行」造主体标识（调用点拿不到现成 id 时用）。"""
    from src.domains.insight.observability.master_usage import new_run_id as _impl
    return _impl(prefix)


__all__ += ["redact_issue_text", "normalize_tokens", "pipeline_trace_id",
            "record_task_enqueued", "record_task_dispatched", "record_task_terminal",
            "record_task_failed", "record_llm_usage", "record_usage",
            "task_model_summary", "master_usage_for_task", "master_run", "new_run_id"]
