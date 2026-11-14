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
        "SELECT repo_key FROM grape_t_repo_credentials WHERE cred_type=? "
        "AND (value=? OR value=?) AND repo_key LIKE ? AND repo_key!=?"),
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
    """同一凭据值被哪些仓库共用（凭据泄漏排查用）。"""
    return [r[0] for r in read_all_any("repo_cred_repos_by_value",
                                       (cred_type, values[0], values[1], prefix, exclude_repo))]


def repo_bot_credential_pairs() -> list[tuple]:
    """全部 bot 凭据 (repo_key, 密文 value) 对（config_store 的跨域 JOIN 拆分后需要全量行）。"""
    return [tuple(r) for r in read_all_any("repo_bot_credential_pairs")]


def repo_keys_with_nonempty_credential(cred_type: str = "") -> set[str]:
    """有非空凭据的仓库集合（cred_type 为空 → 不限类型；供本域侧 NOT EXISTS 等价过滤）。"""
    if cred_type:
        rows = read_all_any("repo_cred_nonempty_keys", (cred_type,))
    else:
        rows = read_all_any("repo_cred_nonempty_keys_all")
    return {r[0] for r in rows if r[0]}


__all__ += ["list_repo_credential_rows", "aggregate_llm_usage", "read_one_any", "read_all_any", "exec_any",
            "repo_credential_value", "repo_credential_meta", "upsert_repo_credential_value",
            "clear_repo_credential_value", "any_repo_credential", "repo_keys_sharing_value",
            "repo_keys_with_nonempty_credential"]


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
