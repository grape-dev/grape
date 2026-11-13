"""runtime 域对外 lib（进程内 import，使用 runtime 自己的 DSN）。

约定（见 plan-lib-three-node §11.3，为将来切 Remote 预留）：
- 只返回 JSON 友好类型（dict/list/基础类型），不返回游标/行对象
- 列表函数支持批量或 limit/offset
- 写函数接受 idempotency_key（最终一致 + 补偿，非强事务）
"""
from __future__ import annotations

import json
from typing import Any

from src.kernel import db_registry


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []


def available() -> bool:
    """runtime 库是否可连（供调用方降级判断）。"""
    return db_registry.domain_db("runtime") is not None


def list_runtimes(org_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """运行时列表（mgmt 运行时页用）。"""
    sql = ("SELECT id, name, display_name, runtime_type, status, org_id, owner_id, repo_key, "
           "healthy, last_seen_at, spawn_fail_degraded_at, updated_at "
           "FROM agent_runtimes")
    args: list[Any] = []
    if org_id is not None:
        sql += " WHERE org_id=%s"
        args.append(org_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(int(limit))
    with db_registry.cursor("runtime") as c:
        c.execute(sql, tuple(args))
        return _rows(c)


def get_runtime(runtime_id: int) -> dict[str, Any]:
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT * FROM agent_runtimes WHERE id=%s", (runtime_id,))
        rows = _rows(c)
        return rows[0] if rows else {}


def task_stats(repo_key: str, since: str | None = None) -> dict[str, Any]:
    """任务聚合（insight 面板用；替代之前的跨库 JOIN/子查询）。"""
    sql = ("SELECT status, COUNT(*) AS n FROM agent_tasks WHERE repo_key=%s")
    args: list[Any] = [repo_key]
    if since:
        sql += " AND created_at >= %s"
        args.append(since)
    sql += " GROUP BY status"
    with db_registry.cursor("runtime") as c:
        c.execute(sql, tuple(args))
        return {row["status"]: row["n"] for row in _rows(c)}


def append_task_message(task_id: str, actor: str, content: str,
                        msg_type: str = "message", idempotency_key: str = "") -> dict[str, Any]:
    """写：追加一条任务消息。

    task_messages 现有列：id/task_id/seq/type/tool/content/input_json/output/created_at/actor，
    没有幂等键列，因此去重按 (task_id, actor, type, content) 判断；后续 P3 可加 idempotency_key 列。
    """
    with db_registry.domain_lock("runtime"):
        with db_registry.cursor("runtime") as c:
            c.execute("SELECT id FROM task_messages WHERE task_id=%s AND actor=%s "
                      "AND type=%s AND content=%s LIMIT 1",
                      (task_id, actor, msg_type, content))
            hit = c.fetchone()
            if hit:
                return {"ok": True, "deduped": True, "id": hit[0]}
            c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM task_messages WHERE task_id=%s", (task_id,))
            row = c.fetchone()
            seq = (row[0] if row else 1) or 1
            c.execute("INSERT INTO task_messages (task_id, seq, type, content, actor) "
                      "VALUES (%s,%s,%s,%s,%s)",
                      (task_id, seq, msg_type, content, actor))
            return {"ok": True, "deduped": False, "id": getattr(c, "lastrowid", None), "seq": seq}


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("runtime") as c:
            c.execute("SELECT COUNT(*) FROM agent_runtimes")
            n = c.fetchone()[0]
        return {"ok": True, "runtimes": n, "dsn_configured": bool(db_registry.dsn_of("runtime"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("runtime"))}


__all__ = ["available", "list_runtimes", "get_runtime", "task_stats",
           "append_task_message", "health"]