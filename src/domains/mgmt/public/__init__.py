"""mgmt 域对外 lib（读侧）：允许列表约束的通用单表查询 + 常用封装。

安全约束：
- 只能访问 `_TABLES` 中登记的表与列（不是开放 SQL 入口，避免把任意 SQL 透传给 lib）
- 一律参数化（%s），不接受拼接的 where 片段中的值
- 只返回元组/字典，不返回游标

用途：runtime / insight 进程在 lib 模式下读 mgmt 库（三库三机）。
"""
from __future__ import annotations

from typing import Any

from src.kernel import db_registry

# 表 → 允许读取的列（按需增补；新增列必须先登记）
_TABLES: dict[str, set[str]] = {
    "managed_repos": {
        "repo_key", "repo_owner", "repo_name", "full_name", "webhook_id", "dev_scene",
        "bot_username", "created_at", "is_admin", "gitcode_publish_repo", "deploy_url",
        "host_status", "scene_recommend_config", "org_id", "host_user_id", "webhook_secret",
        "bot_token_status", "host_vcs_username", "host_step", "host_error", "host_updated_at",
        "bot_gitcode_token", "gitcode_webhook_token", "bot_github_token", "github_webhook_token",
    },
    "unified_user_mapping": {
        "user_id", "gitcode_user", "gitcode_nickname", "feishu_open_id", "feishu_name",
        "github_login", "dingtalk_user_id", "updated_at", "app_id", "gitcode_id",
    },
}


def available() -> bool:
    return db_registry.domain_db("mgmt") is not None


def _check(table: str, columns: list[str]) -> None:
    if table not in _TABLES:
        raise ValueError(f"table '{table}' 未在 mgmt lib 允许列表内")
    unknown = [c for c in columns if c not in _TABLES[table]]
    if unknown:
        raise ValueError(f"column(s) {unknown} 未在 '{table}' 允许列表内")


def fetch_rows(table: str, columns: list[str], where: str = "", params: tuple = (),
               order_by: str = "", limit: int = 0) -> list[tuple]:
    """单表查询：`where` 只放列与占位符（不含值），值走 params。"""
    _check(table, columns)
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, params)
        return [tuple(r) for r in c.fetchall()]


def fetch_one(table: str, columns: list[str], where: str = "", params: tuple = (),
              order_by: str = "") -> tuple | None:
    rows = fetch_rows(table, columns, where, params, order_by, limit=1)
    return rows[0] if rows else None


# ---------- 常用封装（覆盖实际调用点，减少调用方拼接） ----------

def get_repo_field(repo_key: str, field: str) -> Any:
    """取仓库某字段（bot_username / org_id / dev_scene …）；不存在返回 None。"""
    row = fetch_one("managed_repos", [field], "repo_key=%s", (repo_key,))
    return row[0] if row else None


def get_repo_org_id(repo_key: str) -> Any:
    """仓库所属 org（managed_repos 无 id 列，按 repo_key 唯一键取）。"""
    row = fetch_one("managed_repos", ["org_id"], "repo_key=%s", (repo_key,), "repo_key")
    return row[0] if row else None


def list_user_ids_by_open_id(open_id: str) -> list[str]:
    rows = fetch_rows("unified_user_mapping", ["user_id"], "feishu_open_id=%s", (open_id,))
    return [r[0] for r in rows]


def get_feishu_open_id(user_id: str) -> str:
    row = fetch_one("unified_user_mapping", ["feishu_open_id"], "user_id=%s", (user_id,))
    return (row[0] or "") if row else ""


def list_all_user_ids() -> list[str]:
    """去重后的全部 user_id（insight 告警分发用）。"""
    rows = fetch_rows("unified_user_mapping", ["user_id"])
    return sorted({r[0] for r in rows if r[0]})


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("mgmt") as c:
            c.execute("SELECT COUNT(*) FROM managed_repos")
            n = c.fetchone()[0]
        return {"ok": True, "managed_repos": n, "dsn_configured": bool(db_registry.dsn_of("mgmt"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("mgmt"))}


__all__ = ["available", "fetch_rows", "fetch_one", "get_repo_field", "get_repo_org_id",
           "list_user_ids_by_open_id", "get_feishu_open_id", "list_all_user_ids", "health"]