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

from typing import Any

from src.kernel import db_registry


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