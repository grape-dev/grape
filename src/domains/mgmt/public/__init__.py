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
    "agent_shared_knowledge": {
        "id", "scope", "repo_key", "agent_type", "issue_num", "file_path", "status",
        "source_seq", "created_by", "created_at", "obs_key", "content_hash", "updated_at",
        "source", "archived", "description", "name", "org_id",
    },
    "scene_market": {
        "id", "name", "display_name", "version", "description", "market_description", "icon",
        "category_id", "is_featured", "like_count", "source", "source_user_id",
        "recommended_template", "nodes_json", "onboarding", "tags", "author", "content_hash",
        "obs_icon_key", "is_active", "created_by", "updated_by", "created_at", "updated_at",
        "scope", "repo_key", "preset_custom_fields", "org_id",
    },
    "autopilot_tasks": {
        "id", "name", "repo_key", "scene_id", "description", "cron", "agent_chain",
        "issue_number", "max_retry", "enabled", "last_run_at", "next_run_at", "created_at",
        "updated_at", "created_by", "agent_type", "instruction", "skill_name",
        "engine_runtime_ids", "custom_fields", "work_branch",
    },
    "grape_accounts": {
        "id", "username", "email", "email_source", "password_hash", "nickname",
        "avatar_url", "status", "token_version", "email_verified", "email_is_placeholder",
        "created_by_oauth", "last_active_at", "last_login_at", "created_at", "updated_at",
        "username_lower",
    },
    "unified_user_mapping": {
        "user_id", "gitcode_user", "gitcode_nickname", "feishu_open_id", "feishu_name",
        "github_login", "dingtalk_user_id", "updated_at", "app_id", "gitcode_id",
    },
}


def available() -> bool:
    """本域 lib 是否可用：**必须配置了本域自有 DSN**（`uses_own_dsn`）。

    收紧原因（架构审查 F2）：lib 写原语使用 MySQL 方言（`%s` / `ON DUPLICATE KEY` /
    `NOW()`）。若允许"复用进程库"的路径也返回可用，在 sqlite 后端（单实例/测试拓扑）
    开启 lib 开关会导致绑定参数错误。要求自有 DSN 后，lib 只在三库三机拓扑下生效，
    复用进程库时一律走调用方原有 SQL 兜底。
    """
    from src.kernel import db_registry
    return db_registry.uses_own_dsn("mgmt")


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


def list_baseline_knowledge_files(repo_key: str) -> list[tuple]:
    """基线知识（scope='baseline'）的 (file_path, obs_key)，按 id 倒序。"""
    return fetch_rows(
        "agent_shared_knowledge", ["file_path", "obs_key"],
        "repo_key=%s AND scope='baseline' AND obs_key<>'' AND obs_key IS NOT NULL AND archived=0",
        (repo_key,), "id DESC",
    )


def list_knowledge_obs_keys(repo_key: str, scope: str, agent_type: str, issue_num: int) -> list[str]:
    """按（仓库/scope/agent/issue）取非空 obs_key（事件清理时删对象用）。"""
    rows = fetch_rows(
        "agent_shared_knowledge", ["obs_key"],
        "repo_key=%s AND scope=%s AND agent_type=%s AND issue_num=%s "
        "AND obs_key<>'' AND obs_key IS NOT NULL",
        (repo_key, scope, agent_type, issue_num), "id DESC",
    )
    return [r[0] for r in rows if r[0]]


def get_autopilot_task_brief(task_id: int) -> tuple | None:
    """取自动化任务 (repo_key, name)；不存在返回 None（跨域 JOIN 拆解用）。"""
    row = fetch_one("autopilot_tasks", ["repo_key", "name"], "id=%s", (int(task_id),))
    return tuple(row) if row else None


def mark_default_featured(names: list[str]) -> int:
    """把指定 official 场景标记为精选（幂等：已精选的行重复更新无副作用）。"""
    names = [n for n in (names or []) if n]
    if not names:
        return 0
    ph = ", ".join(["%s"] * len(names))
    sql = (f"UPDATE scene_market SET is_featured=1, updated_at=NOW() "
           f"WHERE name IN ({ph}) AND source='official' AND is_active=1")
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, tuple(names))
        return int(c.rowcount or 0)


_SCENE_CONTENT_COLS = (
    "display_name", "version", "description", "market_description", "icon", "category_id",
    "recommended_template", "nodes_json", "onboarding", "tags", "author", "content_hash",
    "obs_icon_key", "preset_custom_fields",
)
_SCENE_REQUIRED = ("name", "source", "scope")


def _scene_where(scene_id: str | None, row_id: int | None, repo_key: str | None):
    """按（scene_id / id / repo_key 组合）定位场景行，返回 (where, params)。"""
    parts, params = [], []
    if row_id is not None:
        parts.append("id=%s"); params.append(int(row_id))
    if scene_id:
        parts.append("scene_id=%s"); params.append(scene_id)
    if repo_key:
        parts.append("repo_key=%s"); params.append(repo_key)
    if not parts:
        raise ValueError("必须给 scene_id / id / repo_key 之一")
    return " AND ".join(parts), tuple(params)


def set_scene_deploy_url(deploy_url: str, scene_id: str | None = None, row_id: int | None = None,
                         repo_key: str | None = None) -> int:
    """更新场景部署地址（幂等：值相同则 0 行变更）。"""
    where, params = _scene_where(scene_id, row_id, repo_key)
    with db_registry.cursor("mgmt") as c:
        c.execute(f"UPDATE devops_pipeline_scene SET deploy_url=%s WHERE {where}",
                  (deploy_url,) + params)
        return int(c.rowcount or 0)


def set_scene_nodes(nodes_json: str, scene_id: str | None = None, row_id: int | None = None,
                    repo_key: str | None = None, expected_hash: str | None = None) -> dict[str, Any]:
    """更新场景节点（内容哈希幂等；可带 expected_hash 防并发覆盖）。

    返回 {"action": updated|skipped|mismatch, "changed": n, "hash": ...}
    """
    import hashlib

    new_hash = hashlib.sha1(nodes_json.encode("utf-8")).hexdigest()[:12]
    where, params = _scene_where(scene_id, row_id, repo_key)
    with db_registry.cursor("mgmt") as c:
        c.execute(f"SELECT nodes_json FROM devops_pipeline_scene WHERE {where} LIMIT 1", params)
        row = c.fetchone()
        if row is None:
            return {"action": "mismatch", "changed": 0, "hash": new_hash}
        old_raw = row[0] or ""
        old_hash = hashlib.sha1(old_raw.encode("utf-8")).hexdigest()[:12] if old_raw else ""
        if old_hash == new_hash:
            return {"action": "skipped", "changed": 0, "hash": new_hash}
        if expected_hash and old_hash != expected_hash:
            return {"action": "mismatch", "changed": 0, "hash": new_hash}
        c.execute(f"UPDATE devops_pipeline_scene SET nodes_json=%s, updated_at=NOW() WHERE {where}",
                  (nodes_json,) + params)
        return {"action": "updated", "changed": int(c.rowcount or 0), "hash": new_hash}


def upsert_scene_content(name: str, payload: dict[str, Any], source: str = "official",
                         scope: str = "official", repo_key: str = "", source_user_id: str = "") -> dict[str, Any]:
    """写入场景内容（只写内容列，**绝不触碰 is_active / is_featured**）。

    返回 {"action": inserted|updated|skipped, "id": ..., "changed": n}。
    幂等：payload 的 content_hash 与库内一致时直接 skipped。
    """
    cols = [c for c in _SCENE_CONTENT_COLS if c in payload]
    with db_registry.cursor("mgmt") as cur:
        cur.execute("SELECT id, content_hash FROM scene_market "
                    "WHERE name=%s AND source=%s LIMIT 1", (name, source))
        row = cur.fetchone()
        if row:
            sid, old_hash = row[0], (row[1] or "")
            if payload.get("content_hash") and payload.get("content_hash") == old_hash:
                return {"action": "skipped", "id": sid, "changed": 0}
            if not cols:
                return {"action": "skipped", "id": sid, "changed": 0}
            sets = ", ".join(f"{c}=%s" for c in cols)
            cur.execute(f"UPDATE scene_market SET {sets}, updated_at=NOW() WHERE id=%s",
                        tuple(payload[c] for c in cols) + (sid,))
            return {"action": "updated", "id": sid, "changed": int(cur.rowcount or 0)}
        # 插入：只带白名单里出现的列 + 定位列
        insert_cols = ["name", "source", "scope"] + cols + ["repo_key", "source_user_id"]
        values = [name, source, scope] + [payload[c] for c in cols] + [repo_key or None, source_user_id or None]
        ph = ", ".join(["%s"] * len(insert_cols))
        cur.execute(f"INSERT INTO scene_market ({', '.join(insert_cols)}) VALUES ({ph})", tuple(values))
        return {"action": "inserted", "id": int(cur.lastrowid or 0), "changed": 1}


def set_scene_icon(name: str, obs_icon_key: str, source: str = "official") -> int:
    """单独设置场景图标（OBS 上传成功后调用），幂等。"""
    with db_registry.cursor("mgmt") as cur:
        cur.execute("UPDATE scene_market SET obs_icon_key=%s, updated_at=NOW() "
                    "WHERE name=%s AND source=%s", (obs_icon_key, name, source))
        return int(cur.rowcount or 0)


def get_official_scene_id(scene_name: str) -> Any:
    """按场景名取 official 模板的 id（无则 None）。"""
    row = fetch_one("scene_market", ["id"], "name=%s AND source='official'", (scene_name,))
    return row[0] if row else None


def count_rows(table: str, where: str = "", params: tuple = ()) -> int:
    """计数（表必须在白名单内；不支持任意列表达式，只有 COUNT(*)。"""
    if table not in _TABLES:
        raise ValueError(f"table '{table}' 未在 mgmt lib 允许列表内")
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, params)
        row = c.fetchone()
        return int(row[0]) if row else 0


def count_official_scenes(featured_only: bool = False) -> int:
    where = "source='official' AND is_active=1"
    if featured_only:
        where += " AND is_featured=1"
    return count_rows("scene_market", where)


def list_enabled_autopilot_tasks() -> list[tuple]:
    """启用中的自动化任务（调度器用），列序与 scheduler 原查询一致。"""
    return fetch_rows(
        "autopilot_tasks",
        ["id", "name", "repo_key", "agent_type", "instruction", "skill_name",
         "cron", "max_retry", "created_by", "engine_runtime_ids", "custom_fields", "work_branch"],
        "enabled=1", (),
    )


def get_account_id_by_username(username: str) -> str | None:
    """按用户名（大小写不敏感）取账号 id。

    注意：grape_accounts.id 是 **UUID 字符串**（不是自增整数），因此返回 str。
    """
    if not username:
        return None
    row = fetch_one("grape_accounts", ["id"], "username_lower=LOWER(%s)", (username,))
    if row is None:
        row = fetch_one("grape_accounts", ["id"], "LOWER(username)=LOWER(%s)", (username,))
    return str(row[0]) if row and row[0] not in (None, "") else None


def get_account_username_by_id(account_id: str) -> str:
    """按账号 id（UUID 字符串）取用户名；不存在返回空串。"""
    if not account_id:
        return ""
    row = fetch_one("grape_accounts", ["username"], "id=%s", (str(account_id),))
    return (row[0] or "") if row else ""


def find_repo_key_in(candidates: list[str]) -> str | None:
    """在候选 repo_key 中找出库里实际存在的那个（跨平台前缀兼容）。"""
    cands = [c for c in (candidates or []) if c]
    if not cands:
        return None
    ph = ", ".join(["%s"] * len(cands))
    rows = fetch_rows("managed_repos", ["repo_key"], f"repo_key IN ({ph})", tuple(cands), "", 1)
    return rows[0][0] if rows else None


def list_alert_user_ids(limit: int = 200) -> list[int]:
    """告警收件人：有 feishu_open_id 且 user_id 有效的用户（去重、截断）。"""
    rows = fetch_rows(
        "unified_user_mapping", ["user_id"],
        "feishu_open_id<>'' AND user_id IS NOT NULL AND user_id<>'' AND user_id<>'0'",
        (), "", limit,
    )
    out: list[int] = []
    for r in rows:
        try:
            out.append(int(r[0]))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def get_user_id_by_mapping_column(column: str, value: str) -> str | None:
    """按映射列（gitcode_user / github_login / feishu_open_id …）反查 user_id。"""
    row = fetch_one(
        "unified_user_mapping", ["user_id"],
        f"{column}=%s AND {column}<>'' AND user_id<>'' AND user_id<>'0'",
        (value,),
    )
    return str(row[0]) if row and row[0] else None


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("mgmt") as c:
            c.execute("SELECT COUNT(*) FROM managed_repos")
            n = c.fetchone()[0]
        return {"ok": True, "managed_repos": n, "dsn_configured": bool(db_registry.dsn_of("mgmt"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("mgmt"))}


__all__ = ["available", "fetch_rows", "fetch_one", "get_repo_field", "get_repo_org_id",
           "list_user_ids_by_open_id", "get_feishu_open_id", "list_all_user_ids",
           "find_repo_key_in", "list_alert_user_ids", "get_user_id_by_mapping_column",
           "get_account_id_by_username", "get_account_username_by_id",
           "upsert_scene_content", "set_scene_icon", "set_scene_deploy_url", "set_scene_nodes", "mark_default_featured", "list_baseline_knowledge_files", "list_knowledge_obs_keys",
           "get_official_scene_id", "count_rows", "count_official_scenes", "list_enabled_autopilot_tasks", "get_autopilot_task_brief",
           "health"]