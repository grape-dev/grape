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


# ---------- 写原语（跨域写；调用方仍需 cross_write.submit 做幂等/补偿） ----------

# 允许外部域（runtime/insight 进程）写入的 managed_repos 列。
# 只登记实际接线用到的列；新增列必须先在此登记再接线（避免 lib 变成任意写入口）。
_REPO_WRITABLE = {"deploy_url", "bot_token_status"}


def set_repo_fields(repo_key: str, fields: dict[str, Any]) -> int:
    """更新 managed_repos 指定列，返回变更行数（幂等：值相同则 0 行）。

    - 只允许 `_REPO_WRITABLE` 内的列，其余列直接 ValueError（不静默忽略）；
    - repo 不存在时返回 0 —— 与调用方原 `UPDATE ... WHERE repo_key=?` 的 rowcount 语义一致。
    """
    unknown = [k for k in fields if k not in _REPO_WRITABLE]
    if unknown:
        raise ValueError(f"column(s) {unknown} not writable through mgmt lib")
    cols = [k for k in fields if k in _REPO_WRITABLE]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=%s" for c in cols)
    with db_registry.cursor("mgmt") as c:
        c.execute(f"UPDATE managed_repos SET {sets} WHERE repo_key=%s",
                  tuple(fields[c] for c in cols) + (repo_key,))
        return int(c.rowcount or 0)


def ensure_repo_row(repo_key: str, repo_owner: str, repo_name: str, full_name: str = "") -> int:
    """幂等补录 managed_repos 最小行（已存在则不动），返回 1=新建 / 0=已存在。"""
    with db_registry.cursor("mgmt") as c:
        c.execute(
            "INSERT IGNORE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES (%s, %s, %s, %s)",
            (repo_key, repo_owner, repo_name, full_name or f"{repo_owner}/{repo_name}"))
        return int(c.rowcount or 0)


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
                         repo_key: str | None = None, require_active: bool = False) -> int:
    """更新场景部署地址（幂等：值相同则 0 行变更）。

    require_active=True 时只改 `is_active=1` 的场景行 —— 与 SRE 收尾登记
    （event_pipeline.register_scene_deploy_url）原 SQL 的 where 口径一致。
    """
    where, params = _scene_where(scene_id, row_id, repo_key)
    if require_active:
        where += " AND is_active=1"
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


# ---------- 用户映射 / 外部身份写原语（P3-5c，均为 runtime/IM 代码写 mgmt 基表） ----------

def upsert_user_mapping_feishu(user_id: str, open_id: str, feishu_name: str, app_id: str) -> dict[str, Any]:
    """建行（INSERT IGNORE）+ 回写飞书三元组（open_id/name/app_id），与原实现两条 SQL 等价。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT IGNORE INTO unified_user_mapping (user_id) VALUES (%s)", (str(user_id),))
        created = int(c.rowcount or 0)
        c.execute("UPDATE unified_user_mapping SET feishu_open_id=%s, feishu_name=%s, app_id=%s, "
                  "updated_at=NOW() WHERE user_id=%s",
                  (open_id, feishu_name, app_id, str(user_id)))
        return {"action": "updated", "created": created, "changed": int(c.rowcount or 0)}


def clear_user_mapping_feishu(user_id: str = "", app_id: str = "") -> int:
    """清空飞书映射（user_id 或 app_id 二选一定位；两者都给则同时约束）。返回变更行数。"""
    if not user_id and not app_id:
        raise ValueError("user_id / app_id 至少给一个")
    sets = "feishu_open_id='', feishu_name='', app_id='', updated_at=NOW()"
    if app_id and not user_id:
        # app 释放：条件里必须带 app_id（原 SQL 同）
        sql = f"UPDATE unified_user_mapping SET {sets} WHERE app_id=%s"
        params: tuple = (app_id,)
    else:
        sql = f"UPDATE unified_user_mapping SET {sets} WHERE user_id=%s"
        params = (str(user_id),)
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, params)
        return int(c.rowcount or 0)


def set_user_mapping_nickname(user_id: str, nickname: str) -> dict[str, Any]:
    """建行（INSERT IGNORE）+ 更新 gitcode_nickname。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT IGNORE INTO unified_user_mapping (user_id) VALUES (%s)", (str(user_id),))
        created = int(c.rowcount or 0)
        c.execute("UPDATE unified_user_mapping SET gitcode_nickname=%s, updated_at=NOW() "
                  "WHERE user_id=%s", (nickname or "", str(user_id)))
        return {"action": "updated", "created": created, "changed": int(c.rowcount or 0)}


def delete_feishu_onboard_task(app_id: str) -> int:
    """删除该 app 的飞书扫码接入任务（含 app_secret/qr_url 敏感数据）。"""
    if not app_id:
        return 0
    with db_registry.cursor("mgmt") as c:
        c.execute("DELETE FROM feishu_onboard_task WHERE app_id=%s", (app_id,))
        return int(c.rowcount or 0)


def mark_external_identity_expired(grape_user_id: str, provider: str) -> int:
    """凭据失效：external_identities.status 1 → 4（只改 status=1 的行，与原 SQL 同）。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE external_identities SET status=4 "
                  "WHERE grape_user_id=%s AND provider=%s AND status=1",
                  (str(grape_user_id), provider))
        return int(c.rowcount or 0)


# ---------- 巡检任务写原语（P3-5d；autopilot_tasks 是 mgmt 基表） ----------

def set_autopilot_run_info(task_id: int, last_run_at: str, next_run_at: str) -> int:
    """更新巡检任务的运行时间（updated_at 与原实现一致，取 last_run_at）。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE autopilot_tasks SET last_run_at=%s, next_run_at=%s, updated_at=%s "
                  "WHERE id=%s", (last_run_at, next_run_at, last_run_at, int(task_id)))
        return int(c.rowcount or 0)


def set_autopilot_engine_ids(task_id: int, engine_runtime_ids_json: str) -> int:
    """更新巡检任务绑定的引擎列表（JSON 字符串，原样写回）。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE autopilot_tasks SET engine_runtime_ids=%s WHERE id=%s",
                  (engine_runtime_ids_json, int(task_id)))
        return int(c.rowcount or 0)


# ---------- 知识库写原语（P3-5b：agent_shared_knowledge / scene_knowledge_ref / scene_skill_ref） ----------

def upsert_baseline_knowledge(repo_key: str, display_name: str, description: str,
                              file_path: str, obs_key: str, content_hash: str) -> dict[str, Any]:
    """baseline 知识 upsert（定位 repo_key + scope='baseline' + name + archived=0）。

    与 `runtime/project_info/builder._write_to_obs` 原 SQL 等价；返回 {"action","id"}，
    id 供 `scene_knowledge_ref` 关联使用（原实现同样依赖 lastrowid / 命中行 id）。
    """
    with db_registry.cursor("mgmt") as c:
        c.execute("SELECT id FROM agent_shared_knowledge "
                  "WHERE repo_key=%s AND scope='baseline' AND name=%s AND archived=0 LIMIT 1",
                  (repo_key, display_name))
        row = c.fetchone()
        if row:
            kid = int(row[0])
            c.execute("UPDATE agent_shared_knowledge SET status='accepted', obs_key=%s, "
                      "content_hash=%s, created_by='system', source='system', name=%s, "
                      "description=%s WHERE id=%s",
                      (obs_key, content_hash, display_name, description, kid))
            return {"action": "updated", "id": kid, "changed": int(c.rowcount or 0)}
        c.execute("INSERT INTO agent_shared_knowledge "
                  "(scope, repo_key, agent_type, issue_num, file_path, status, obs_key, "
                  "content_hash, created_by, source, name, description) "
                  "VALUES ('baseline', %s, '', 0, %s, 'accepted', %s, %s, 'system', 'system', %s, %s)",
                  (repo_key, file_path, obs_key, content_hash, display_name, description))
        return {"action": "inserted", "id": int(c.lastrowid or 0), "changed": 1}


def ensure_scene_knowledge_ref(knowledge_id: int, scene_id: str = "", agent_type: str = "",
                               issue_num: int = 0, created_by: str = "system") -> int:
    """幂等建立 scene_knowledge_ref 关联（INSERT IGNORE），返回 1=新建 / 0=已存在。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT IGNORE INTO scene_knowledge_ref "
                  "(knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
                  "VALUES (%s, %s, %s, %s, 1, %s)",
                  (int(knowledge_id), scene_id, agent_type, int(issue_num), created_by))
        return int(c.rowcount or 0)


def delete_event_knowledge(repo_key: str, scope: str, agent_type: str, issue_num: int) -> dict[str, int]:
    """删除某事件的知识记录：先按 knowledge_id 删 ref，再删主表（顺序与原实现一致）。"""
    with db_registry.cursor("mgmt") as c:
        params = (repo_key, scope, agent_type, int(issue_num))
        c.execute("SELECT id FROM agent_shared_knowledge WHERE repo_key=%s AND scope=%s "
                  "AND agent_type=%s AND issue_num=%s", params)
        kids = [int(r[0]) for r in c.fetchall()]
        refs_deleted = 0
        if kids:
            ph = ",".join(["%s"] * len(kids))
            c.execute(f"DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ({ph})", tuple(kids))
            refs_deleted = int(c.rowcount or 0)
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=%s AND scope=%s "
                  "AND agent_type=%s AND issue_num=%s", params)
        return {"knowledge": len(kids), "refs_deleted": refs_deleted,
                "rows_deleted": int(c.rowcount or 0)}


def replace_scene_skill_ref(scene_id: int, refs: list[dict[str, Any]]) -> dict[str, int]:
    """重建某场景的技能引用：先删该 scene_id 全部，再 INSERT IGNORE 逐条（幂等）。

    refs: [{"skill_id": int, "skill_name": str}]
    """
    with db_registry.cursor("mgmt") as c:
        c.execute("DELETE FROM scene_skill_ref WHERE scene_id=%s", (int(scene_id),))
        deleted = int(c.rowcount or 0)
        inserted = 0
        for r in refs or []:
            c.execute("INSERT IGNORE INTO scene_skill_ref (scene_id, skill_id, skill_name) "
                      "VALUES (%s, %s, %s)",
                      (int(scene_id), int(r.get("skill_id") or 0), str(r.get("skill_name") or "")))
            inserted += int(c.rowcount or 0)
        return {"deleted": deleted, "inserted": inserted}


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


# ---------- 命名查询白名单（P4 读路径：runtime 域代码读 mgmt 表） ----------
_READ_QUERIES: dict[str, str] = {
    "grape_user_id_by_external_id": (
        "SELECT ei.grape_user_id FROM external_identities ei "
        "WHERE ei.provider=%s AND ei.status=1 AND ei.external_id=%s LIMIT 1"),
    "grape_user_id_by_external_login": (
        "SELECT ei.grape_user_id FROM external_identities ei "
        "WHERE ei.provider=%s AND ei.external_login=%s AND ei.status=1 LIMIT 1"),
    "knowledge_id_baseline": (
        "SELECT id FROM agent_shared_knowledge "
        "WHERE repo_key=%s AND scope='baseline' AND name=%s AND archived=0 LIMIT 1"),
    "managed_repos_3564e6": "SELECT bot_username FROM managed_repos WHERE repo_key=%s AND bot_username!='' LIMIT 1",
    "managed_repos_e9a379": "SELECT org_id FROM managed_repos WHERE repo_key=%s ORDER BY org_id DESC LIMIT 1",
    "managed_repos_6f73d4": "SELECT repo_key FROM managed_repos WHERE repo_key=%s LIMIT 1",
    "managed_repos_b939bc": "SELECT repo_owner, repo_name FROM managed_repos WHERE repo_key=%s",
    # 用户映射（runtime / insight 代码读）
    "um_feishu_open_ids_all": (
        "SELECT DISTINCT user_id FROM unified_user_mapping WHERE feishu_open_id!='' "
        "AND user_id IS NOT NULL AND user_id NOT IN ('', '0')"),
    "um_feishu_open_id_by_user": (
        "SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=%s AND feishu_open_id!='' LIMIT 1"),
    "um_row_by_feishu_open_id": (
        "SELECT user_id, gitcode_user, feishu_name, gitcode_nickname "
        "FROM unified_user_mapping WHERE feishu_open_id=%s LIMIT 1"),
    # 仓库成员
    "user_ids_by_repo": "SELECT DISTINCT user_id FROM user_repos WHERE repo_key=%s",
    "first_join_user_by_repo": (
        "SELECT user_id FROM user_repos WHERE (repo_key=%s OR repo_key=%s) AND is_join=1 "
        "ORDER BY user_id LIMIT 1"),
    # 巡检任务
    "autopilot_tasks_enabled_brief": (
        "SELECT id, name, engine_runtime_ids FROM autopilot_tasks WHERE enabled=1"),
    "autopilot_tasks_enabled_full": (
        "SELECT id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, "
        "created_by, engine_runtime_ids, custom_fields, work_branch FROM autopilot_tasks WHERE enabled=1"),
    "autopilot_task_last_run": "SELECT last_run_at FROM autopilot_tasks WHERE id=%s",
    "scene_market_official_by_name": (
        "SELECT id, version, content_hash, is_active FROM scene_market "
        "WHERE name=%s AND source='official' LIMIT 1"),
    "scene_market_featured_count": (
        "SELECT COUNT(*) FROM scene_market WHERE source='official' AND is_featured=1 AND is_active=1"),
    "knowledge_ids_scoped": (
        "SELECT id FROM agent_shared_knowledge "
        "WHERE repo_key=%s AND scope=%s AND agent_type=%s AND issue_num=%s"),
}


def read_one(query: str, params: tuple = ()) -> Any | None:
    """按白名单执行单行读；未知查询名抛 KeyError（不静默）。"""
    sql = _READ_QUERIES.get(query)
    if sql is None:
        raise KeyError(f"unknown mgmt read query: {query}")
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, params)
        return c.fetchone()


def read_all(query: str, params: tuple = ()) -> list[Any]:
    """按白名单执行多行读；未知查询名抛 KeyError。"""
    sql = _READ_QUERIES.get(query)
    if sql is None:
        raise KeyError(f"unknown mgmt read query: {query}")
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, params)
        return list(c.fetchall() or [])
