"""mgmt 域对外 lib（读侧）：允许列表约束的通用单表查询 + 常用封装。

安全约束：
- 只能访问 `_TABLES` 中登记的表与列（不是开放 SQL 入口，避免把任意 SQL 透传给 lib）
- 一律参数化（%s），不接受拼接的 where 片段中的值
- 只返回元组/字典，不返回游标

用途：runtime / insight 进程在 lib 模式下读 mgmt 库（三库三机）。
"""
from __future__ import annotations

from contextlib import contextmanager

from src.kernel.config import log

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


def account_auth_row(username: str, conn: Any = None) -> tuple | None:
    """按注册名（大小写不敏感，仅 status=1）取 (id, nickname)。

    conn 非空 → 在调用方连接上执行（单库 / 视图期，kernel 传入本进程库）；
    None → 走 mgmt 自有 DSN（三库独立）。不存在返回 None。
    """
    if not username:
        return None
    sql = ("SELECT id, nickname FROM grape_accounts "
           "WHERE LOWER(username)=LOWER(?) AND status=1 LIMIT 1")
    if conn is not None:
        return conn.execute(sql, (username,)).fetchone()
    with db_registry.cursor("mgmt") as c:
        c.execute(sql.replace("?", "%s"), (username,))
        return c.fetchone()


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
        except Exception as _e:
            log.debug(f"src/domains/mgmt/public/__init__.py best-effort skipped: {_e}")
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


# ---------- P2 服务接口补齐（N6/N8/N9/N10） ----------

# N6：仓库公开字段白名单（**不含任何 token/secret 明文**）
_REPO_PUBLIC_COLS = ("repo_key", "repo_owner", "repo_name", "full_name", "webhook_id",
                     "dev_scene", "host_status", "bot_username", "org_id", "deploy_url",
                     "bot_token_status", "host_vcs_username", "created_at", "is_admin")


def repo_bundle(repo_key: str) -> dict[str, Any]:
    """仓库配置 + 凭据状态 + webhook 配置 + 场景，一次返回（N6）。

    凭据只返回「有/无」，不返回明文；替代 runtime 侧 3 次单点调用。
    """
    row = fetch_one("managed_repos", list(_REPO_PUBLIC_COLS), "repo_key=%s", (repo_key,))
    if not row:
        return {}
    out: dict[str, Any] = dict(zip(_REPO_PUBLIC_COLS, row))
    secret_cols = ("gitcode_webhook_token", "webhook_secret", "bot_gitcode_token",
                   "bot_github_token", "github_webhook_token")
    flags = fetch_one("managed_repos", list(secret_cols), "repo_key=%s", (repo_key,))
    if flags:
        out["credentials"] = {c: bool(flags[i]) for i, c in enumerate(secret_cols)}
        out["credentials"] = {k.replace("_token", "").replace("has_", ""): v
                              for k, v in out["credentials"].items()}
    else:
        out["credentials"] = {}
    with db_registry.cursor("mgmt") as c:
        c.execute("SELECT scene_id, name, is_active FROM devops_pipeline_scene "
                  "WHERE repo_key=%s AND is_active=1 ORDER BY name", (repo_key,))
        out["scenes"] = [{"scene_id": r[0], "name": r[1] or r[0], "is_active": r[2]}
                         for r in c.fetchall()]
    return out


_ACCOUNT_PUBLIC_COLS = ("id", "username", "nickname", "status")


def get_account_public(account_id: str) -> dict[str, Any] | None:
    """账号只读（N8）：仅 id/username/display_name/status/org_id，禁止口令列。"""
    row = fetch_one("grape_accounts", list(_ACCOUNT_PUBLIC_COLS), "id=%s", (account_id,))
    if not row:
        return None
    out = {"id": row[0], "username": row[1], "display_name": row[2] or row[1],
           "status": row[3], "org_id": None}
    with db_registry.cursor("mgmt") as c:
        c.execute("SELECT org_id FROM org_member WHERE user_id=%s ORDER BY org_id LIMIT 1",
                  (account_id,))
        org = c.fetchone()
        out["org_id"] = org[0] if org else None
    return out


_KNOWLEDGE_PUBLIC_COLS = ("id", "name", "description", "scope", "repo_key", "agent_type",
                          "issue_num", "status", "updated_at", "content_hash", "obs_key",
                          "file_path")


def get_knowledge_entry(knowledge_id: int) -> dict[str, Any] | None:
    """知识条目（N9）：返回索引元数据；正文仍由 owner 域文件/对象存储读取。"""
    row = fetch_one("agent_shared_knowledge", list(_KNOWLEDGE_PUBLIC_COLS),
                    "id=%s", (int(knowledge_id),))
    if not row:
        return None
    out: dict[str, Any] = dict(zip(_KNOWLEDGE_PUBLIC_COLS, row))
    out["display_name"] = out.get("name") or ""
    out["has_content"] = bool(out.pop("obs_key", "") or out.get("file_path"))
    return out


# N10：平台 → unified_user_mapping 列（新增平台必须在此登记）
_MAPPING_COLUMNS = {
    "feishu": "feishu_open_id",
    "gitcode": "gitcode_user",
    "github": "github_login",
    "dingtalk": "dingtalk_user_id",
}


def user_mapping_batch(keys: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    """批量外部身份 → grape user_id（N10）；批量 ≤ 200 键，保序返回。"""
    keys = list(keys or [])[: max(1, min(int(limit), 200))]
    out: list[dict[str, Any]] = []
    by_platform: dict[str, list[str]] = {}
    for k in keys:
        platform = str((k or {}).get("platform") or "").strip().lower()
        external = str((k or {}).get("external_id") or "").strip()
        by_platform.setdefault(platform, []).append(external)
    found: dict[tuple[str, str], str] = {}
    for platform, values in by_platform.items():
        column = _MAPPING_COLUMNS.get(platform)
        if not column:
            continue
        values = [v for v in dict.fromkeys(values) if v]
        if not values:
            continue
        marks = ",".join(["%s"] * len(values))
        sql = (f"SELECT {column}, user_id FROM unified_user_mapping "
               f"WHERE {column} IN ({marks}) AND user_id IS NOT NULL AND user_id NOT IN ('', '0')")
        with db_registry.cursor("mgmt") as c:
            c.execute(sql, tuple(values))
            for ext, uid in c.fetchall():
                found.setdefault((platform, ext), uid)
    for k in keys:
        platform = str((k or {}).get("platform") or "").strip().lower()
        external = str((k or {}).get("external_id") or "").strip()
        uid = found.get((platform, external))
        out.append({"platform": platform, "external_id": external,
                    "user_id": uid, "matched": bool(uid)})
    return out


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("mgmt") as c:
            c.execute("SELECT COUNT(*) FROM managed_repos")
            n = c.fetchone()[0]
        return {"ok": True, "managed_repos": n, "dsn_configured": bool(db_registry.dsn_of("mgmt"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("mgmt"))}


__all__ = ['installation_active_row', 'installation_config_by_app', 'installation_activate', 'installation_revoke', 'installation_active_configs', 'installation_set_config', 'installation_active_installs', "unified_user_mapping_ddl", "available", "fetch_rows", "fetch_one", "get_repo_field", "get_repo_org_id",
           "list_user_ids_by_open_id", "get_feishu_open_id", "list_all_user_ids",
           "find_repo_key_in", "list_alert_user_ids", "get_user_id_by_mapping_column",
           "get_account_id_by_username", "get_account_username_by_id",
           # 用户映射（unified_user_mapping）双拓扑读写薄封装（conn=None → 自有 DSN）
           "resolve_feishu_open_id", "list_user_mappings", "list_feishu_name_open_id_pairs",
           "bind_feishu_mapping_row", "sync_user_mapping", "ensure_user_mapping_schema",
           "upsert_scene_content", "set_scene_icon", "set_scene_deploy_url", "set_scene_nodes", "mark_default_featured", "list_baseline_knowledge_files", "list_knowledge_obs_keys",
           "get_official_scene_id", "count_rows", "count_official_scenes", "list_enabled_autopilot_tasks", "get_autopilot_task_brief",
           # P2 服务接口补齐（N6/N8/N9/N10）
           "repo_bundle", "get_account_public", "get_knowledge_entry", "user_mapping_batch",
           # W2 渠道写原语
           "acquire_channel_lease", "renew_channel_lease", "release_channel_lease",
           "release_all_leases", "claim_inbound_dedup", "upsert_channel_installation",
           "upsert_chat_session_binding", "upsert_sender_identity",
           "add_event_action_filter", "update_event_action_filter",
           "delete_event_action_filter",
           # W3 派生写
           "add_user_feedback", "save_inbound_message", "upsert_outbound_card",
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
    "devops_pipeline_scene_9bcf48": "SELECT id, repo_key, scene_id, nodes_json FROM devops_pipeline_scene WHERE is_active=1",
    "devops_pipeline_scene_b5cd89": "SELECT nodes_json FROM devops_pipeline_scene WHERE id=%s",
    "devops_pipeline_scene_b5f6ee": "SELECT scene_id, name FROM devops_pipeline_scene WHERE repo_key=%s AND is_active=1 ORDER BY name",
    "devops_pipeline_scene_ab0110": "SELECT linked_repos, advanced_options FROM devops_pipeline_scene WHERE scene_id=%s AND repo_key=%s AND is_active=1 LIMIT 1",
    "devops_pipeline_scene_b1eaa2": "SELECT scene_id, nodes_json FROM devops_pipeline_scene WHERE repo_key=%s AND scene_id=%s AND is_active=1",
    "devops_pipeline_scene_7a48a2": "SELECT scene_id, nodes_json, advanced_options FROM devops_pipeline_scene WHERE repo_key=%s AND pr_subscription_enabled=1 AND is_active=1",
    "devops_pipeline_scene_8b6d22": "SELECT nodes_json FROM devops_pipeline_scene WHERE repo_key=%s AND scene_id=%s",
    "devops_pipeline_scene_8c7c25": "SELECT scene_id FROM devops_pipeline_scene WHERE scene_id=%s AND repo_key=%s AND is_active=1 LIMIT 1",
    "grape_accounts_3dd020": "SELECT ga.id FROM grape_accounts ga WHERE ga.id IS NOT NULL AND ga.id NOT IN ('', '0') AND ga.status=1 AND EXISTS (SELECT 1 FROM external_identities ei WHERE ei.grape_user_id=ga.id AND ei.provider=%s AND ei.status=1 AND LOWER(ei.external_login)=LOWER(%s) AND ei.external_login!='') LIMIT 1",
    "grape_accounts_d0ac07": "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(%s)",
    "grape_accounts_0b3fdc": "SELECT id FROM grape_accounts WHERE username=%s AND id IS NOT NULL AND id NOT IN ('', '0') LIMIT 1",
    "grape_accounts_e047af": "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(%s) LIMIT 1",
    "autopilot_tasks_e3fa0b": "SELECT id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, created_by, engine_runtime_ids, custom_fields, work_branch FROM autopilot_tasks WHERE id=%s",
    "autopilot_subscriptions_13648d": "SELECT s.user_id, t.name, t.repo_key FROM autopilot_subscriptions s JOIN autopilot_tasks t ON s.task_id=t.id WHERE s.task_id=%s",
    "user_accounts_e2eb04": "SELECT username FROM user_accounts WHERE id=%s",
    "scene_market_c969bb": "SELECT id, name FROM scene_market WHERE source='official'",
    "scene_market_0d2dd0": "SELECT id FROM scene_market WHERE name=%s AND source='official' LIMIT 1",
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


# ---------- W2 渠道写原语（runtime IM 进程写 mgmt 归属表；方案 §3.5 M1–M6） ----------
# 统一约束：函数内 SQL 固定、参数化、幂等（UPDATE→INSERT 或 INSERT IGNORE）、
# 目标表均为 mgmt 归属，调用方（runtime/insight）经 lib 或 N13 端点写入。

def acquire_channel_lease(lease_key: str, token: str, owner: str = "",
                          ttl_seconds: int = 60) -> bool:
    """抢占渠道租约（W2）：条件 UPDATE（空/过期/同 token 重入），无行则 INSERT。

    幂等：同 token 重入返回 True；并发插入撞主键 → False（未抢到）。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    stamp = now.isoformat()
    channel_type = lease_key.split(":", 1)[0] if ":" in lease_key else ""
    with db_registry.cursor("mgmt") as c:
        c.execute(
            "UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
            "lease_expires_at=%s, lease_acquired_at=IFNULL(lease_acquired_at, %s), "
            "updated_at=%s WHERE lease_key=%s AND (lease_token IS NULL OR "
            "lease_token=%s OR lease_expires_at IS NULL OR lease_expires_at < %s)",
            (token, owner, expires, stamp, stamp, lease_key, token, stamp))
        if int(c.rowcount or 0) > 0:
            return True
        try:
            c.execute(
                "INSERT INTO grape_t_channel_lease (lease_key, channel_type, lease_token, "
                "lease_owner, lease_expires_at, lease_acquired_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (lease_key, channel_type, token, owner, expires, stamp, stamp))
            return True
        except Exception as exc:  # noqa: BLE001 - 并发抢占时主键冲突即为失败
            log.info(f"channel lease not acquired ({lease_key}): {exc}")
            return False


def renew_channel_lease(lease_key: str, token: str, owner: str = "", ttl_seconds: int = 60,
                        expected_token: str | None = None) -> bool:
    """租约续约（W2）：仅当 token 为空或等于 expected_token 时写入。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    stamp = now.isoformat()
    if expected_token:
        sql = ("UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
               "lease_expires_at=%s, lease_acquired_at=IFNULL(lease_acquired_at, %s), "
               "updated_at=%s WHERE lease_key=%s AND (lease_token IS NULL OR lease_token=%s)")
        args = (token, owner, expires, stamp, stamp, lease_key, expected_token)
    else:
        sql = ("UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
               "lease_expires_at=%s, lease_acquired_at=IFNULL(lease_acquired_at, %s), "
               "updated_at=%s WHERE lease_key=%s AND lease_token IS NULL")
        args = (token, owner, expires, stamp, stamp, lease_key)
    with db_registry.cursor("mgmt") as c:
        c.execute(sql, args)
        return int(c.rowcount or 0) > 0


def release_channel_lease(lease_key: str, token: str) -> bool:
    """释放租约（W2）：仅当 token 匹配时清空；已释放返回 False（幂等）。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE grape_t_channel_lease SET lease_token=NULL, lease_owner=NULL, "
                  "lease_expires_at=NULL, updated_at=NOW() "
                  "WHERE lease_key=%s AND lease_token=%s", (lease_key, token))
        return int(c.rowcount or 0) > 0


def release_all_leases(pairs: list[tuple[str, str]]) -> int:
    """批量释放 [(lease_key, token)]，返回实际释放条数（W2）。"""
    released = 0
    for lease_key, token in pairs or []:
        if release_channel_lease(lease_key, token):
            released += 1
    return released


def claim_inbound_dedup(installation_id: str, message_id: str, state: str = "claimed",
                        claim_token: str = "") -> bool:
    """入站消息去重占位（W2）：主键 (installation_id, message_id) 首占返回 True。

    唯一键幂等：并发/重放时第二次返回 False，调用方据此丢弃重复消息。
    """
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT IGNORE INTO grape_t_channel_inbound_dedup "
                  "(installation_id, message_id, state, claim_token, created_at) "
                  "VALUES (%s, %s, %s, %s, NOW())",
                  (installation_id, message_id, state, claim_token))
        return int(c.rowcount or 0) > 0


def upsert_channel_installation(installation_id: str, channel_type: str, scope: str,
                                owner_user_id: str = "", repo_key: str = "",
                                config: str = "", status: str = "active",
                                legacy_app_id: str = "") -> dict[str, Any]:
    """安装记录幂等 upsert（W2）：先按 id 更新，未命中再按 (legacy_app_id, scope) 更新，
    仍未命中才插入（id 由调用方给定，重放不会产生第二行）。
    """
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE grape_t_channel_installation SET channel_type=%s, scope=%s, "
                  "owner_user_id=%s, repo_key=%s, config=%s, status=%s, legacy_app_id=%s, "
                  "updated_at=NOW() WHERE id=%s",
                  (channel_type, scope, owner_user_id, repo_key, config, status,
                   legacy_app_id, installation_id))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated", "id": installation_id}
        if legacy_app_id:
            c.execute("UPDATE grape_t_channel_installation SET channel_type=%s, scope=%s, "
                      "owner_user_id=%s, repo_key=%s, config=%s, status=%s, updated_at=NOW() "
                      "WHERE legacy_app_id=%s AND scope=%s",
                      (channel_type, scope, owner_user_id, repo_key, config, status,
                       legacy_app_id, scope))
            if int(c.rowcount or 0) > 0:
                return {"action": "updated_by_legacy_app", "id": installation_id}
        try:
            c.execute("INSERT INTO grape_t_channel_installation "
                      "(id, channel_type, scope, owner_user_id, repo_key, config, status, "
                      "legacy_app_id, created_at, updated_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
                      (installation_id, channel_type, scope, owner_user_id, repo_key, config,
                       status, legacy_app_id))
            return {"action": "inserted", "id": installation_id}
        except Exception as exc:  # noqa: BLE001 - 并发插入撞主键：回退为更新
            log.info(f"installation upsert raced ({installation_id}): {exc}")
            c.execute("UPDATE grape_t_channel_installation SET channel_type=%s, scope=%s, "
                      "owner_user_id=%s, repo_key=%s, config=%s, status=%s, updated_at=NOW() "
                      "WHERE id=%s",
                      (channel_type, scope, owner_user_id, repo_key, config, status,
                       installation_id))
            return {"action": "updated_after_conflict", "id": installation_id}


def upsert_chat_session_binding(installation_id: str, channel_chat_id: str,
                                channel_type: str = "", chat_type: str = "", scope: str = "",
                                repo_key: str = "", agent_type: str = "",
                                runtime_session_id: str = "", config: str = "") -> dict[str, Any]:
    """IM 会话绑定 upsert（W2）：(installation_id, channel_chat_id, chat_type) 唯一语义。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE grape_t_channel_chat_session_binding SET channel_type=%s, scope=%s, "
                  "repo_key=%s, agent_type=%s, runtime_session_id=%s, config=%s, "
                  "last_active_at=NOW(), updated_at=NOW() "
                  "WHERE installation_id=%s AND channel_chat_id=%s AND chat_type=%s",
                  (channel_type, scope, repo_key, agent_type, runtime_session_id, config,
                   installation_id, channel_chat_id, chat_type))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated"}
        c.execute("INSERT INTO grape_t_channel_chat_session_binding "
                  "(installation_id, channel_type, channel_chat_id, chat_type, scope, repo_key, "
                  "agent_type, runtime_session_id, config, last_active_at, created_at, updated_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())",
                  (installation_id, channel_type, channel_chat_id, chat_type, scope, repo_key,
                   agent_type, runtime_session_id, config))
        return {"action": "inserted"}


def upsert_sender_identity(installation_id: str, platform_user_id: str,
                           user_id: str, platform_stable_user_id: str = "") -> dict[str, Any]:
    """发送者身份映射 upsert（W2）：主键 (installation_id, platform_user_id)。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("UPDATE grape_t_channel_sender_identity SET user_id=%s, "
                  "platform_stable_user_id=%s "
                  "WHERE installation_id=%s AND platform_user_id=%s",
                  (user_id, platform_stable_user_id, installation_id, platform_user_id))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated"}
        c.execute("INSERT INTO grape_t_channel_sender_identity "
                  "(installation_id, platform_user_id, platform_stable_user_id, user_id) "
                  "VALUES (%s, %s, %s, %s)",
                  (installation_id, platform_user_id, platform_stable_user_id, user_id))
        return {"action": "inserted"}


# ---------- W2：事件过滤规则（mgmt 归属；写方 runtime/api 经 lib） ----------

_EVENT_FILTER_WRITABLE = ("event_type", "action", "allow", "actor", "require_changes",
                          "block_changes", "handle_side_effect", "priority", "enabled",
                          "vcs_source")


def add_event_action_filter(fields: dict[str, Any], created_at: str) -> int:
    """新增事件过滤规则（W2），返回新行 id；同 event_type×action 由调用方去重。"""
    cols = [c for c in _EVENT_FILTER_WRITABLE if c in fields]
    cols += ["created_at", "updated_at"]
    vals = [fields[c] for c in cols[:-2]] + [created_at, created_at]
    with db_registry.cursor("mgmt") as c:
        c.execute(f"INSERT INTO event_action_filter ({', '.join(cols)}) "
                  f"VALUES ({', '.join(['%s'] * len(cols))})", tuple(vals))
        return int(getattr(c, "lastrowid", 0) or 0)


def update_event_action_filter(rule_id: int, fields: dict[str, Any], updated_at: str) -> int:
    """按白名单列更新规则（W2）；返回受影响行数（0 = 不存在，供 404）。"""
    unknown = [k for k in fields if k not in _EVENT_FILTER_WRITABLE]
    if unknown:
        raise ValueError(f"column(s) {unknown} not writable through mgmt lib")
    cols = [k for k in fields if k in _EVENT_FILTER_WRITABLE]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=%s"
    with db_registry.cursor("mgmt") as c:
        c.execute(f"UPDATE event_action_filter SET {sets} WHERE id=%s",
                  tuple(fields[c] for c in cols) + (updated_at, int(rule_id)))
        return int(c.rowcount or 0)


def delete_event_action_filter(rule_id: int) -> int:
    """删除规则（W2）；返回受影响行数（0 = 不存在）。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("DELETE FROM event_action_filter WHERE id=%s", (int(rule_id),))
        return int(c.rowcount or 0)


# ---------- W3 派生写（outbox 目标域落地；幂等靠唯一键或 action 幂等键） ----------

def add_user_feedback(fields: dict[str, Any], created_at: str) -> int:
    """落一条用户反馈（W3）；返回新行 id。幂等由调用方 action 幂等键保证。"""
    cols = [c for c in ("user_id", "username", "category", "title", "description", "page_url",
                        "repo_key", "user_agent", "attachment_url", "attachment_name",
                        "event_seq", "task_id", "issue_id", "issue_number", "agent_type",
                        "engine_info", "fail_summary", "source", "skill_id", "skill_name",
                        "skill_scope", "issue_url", "feedback_kind") if c in fields]
    cols += ["created_at", "updated_at"]
    vals = [fields[c] for c in cols[:-2]] + [created_at, created_at]
    with db_registry.cursor("mgmt") as c:
        c.execute(f"INSERT INTO user_feedback ({', '.join(cols)}) "
                  f"VALUES ({', '.join(['%s'] * len(cols))})", tuple(vals))
        return int(getattr(c, "lastrowid", 0) or 0)


def save_inbound_message(installation_id: str, message_id: str, chat_id: str = "",
                         sender_id: str = "", text: str = "", created_at: str = "") -> bool:
    """入站消息流水（W3）：主键 (installation_id, message_id) 首写返回 True。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT IGNORE INTO grape_t_channel_inbound_message "
                  "(installation_id, message_id, chat_id, sender_id, text, created_at) "
                  "VALUES (%s, %s, %s, %s, %s, COALESCE(NULLIF(%s, ''), NOW()))",
                  (installation_id, message_id, chat_id, sender_id, text, created_at or ""))
        return int(c.rowcount or 0) > 0


def upsert_outbound_card(out_track_id: str, installation_id: str = "", channel_type: str = "",
                         channel_chat_id: str = "", chat_type: str = "", payload: str = "",
                         status: str = "pending", last_error: str = "") -> int:
    """外发卡片流水 upsert（W3）：主键 out_track_id，重放只更新状态。"""
    with db_registry.cursor("mgmt") as c:
        c.execute("INSERT INTO grape_t_channel_outbound_card "
                  "(out_track_id, installation_id, channel_type, channel_chat_id, chat_type, "
                  "payload, status, last_error, created_at, updated_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()) "
                  "ON DUPLICATE KEY UPDATE status=VALUES(status), last_error=VALUES(last_error), "
                  "payload=IF(VALUES(payload)='', payload, VALUES(payload)), updated_at=NOW()",
                  (out_track_id, installation_id, channel_type, channel_chat_id, chat_type,
                   payload, status, last_error))
        return int(c.rowcount or 0)


# ---------- 用户映射（unified_user_mapping）双拓扑薄封装：kernel/api 进程读写 ----------
# 为什么带 conn 参数：单库/视图期该表就在调用方进程库里（session_manager.db），
# 调用方的 db_lock / 事务语义必须原样保留 → 直接在其连接上执行；
# 三库独立时表只在 mgmt 实例，调用方传 conn=None，本层走 db_registry 的 mgmt 自有 DSN。
# 方言：SQL 一律以 sqlite 方言书写（? 占位 / INSERT OR IGNORE / datetime('now')）——
# MySQLCompatDB 会翻译这三类；conn=None 的原生 pymysql 分支由 _um_sql() 就地翻译。

_UM_NATIVE_SUBS = (("INSERT OR IGNORE", "INSERT IGNORE"),
                   ("datetime('now')", "CAST(NOW() AS CHAR)"))


def _um_sql(sql: str, native: bool) -> str:
    """把以 sqlite 方言书写的**固定** SQL 转成 pymysql 原生方言（值仍走参数）。"""
    if not native:
        return sql
    for src, dst in _UM_NATIVE_SUBS:
        sql = sql.replace(src, dst)
    return sql.replace("?", "%s")


@contextmanager
def _um_cursor(conn: Any = None):
    """产出 (cursor, native_mysql)。给定 conn → 调用方连接（提交/回滚仍归调用方）。"""
    if conn is None:
        with db_registry.cursor("mgmt") as c:
            yield c, True
    else:
        yield conn.cursor(), False


def _um_one(c, native: bool, sql: str, params: tuple = ()):
    c.execute(_um_sql(sql, native), params)
    return c.fetchone()


def resolve_feishu_open_id(login: str, app_id: str = "", conn: Any = None) -> str | None:
    """按平台 login 解析飞书 open_id（回退链与原 session_manager.get_feishu_open_id 一致）。

    FR-20.8：gitcode_user 未命中**或命中但 open_id 空**→ 回退 github_login（GitHub-only 用户）；
    FR-22.5：两者都未命中 → 注册名查 grape_accounts.id → 按 user_id 查 um 行；
    app_id 非空 → 只认该 App 的 open_id（open_id 为 app 级）。
    """
    if not login:
        return None
    with _um_cursor(conn) as (c, native):
        if app_id:
            row = _um_one(c, native,
                          "SELECT feishu_open_id FROM unified_user_mapping "
                          "WHERE gitcode_user=? AND app_id=?", (login, app_id))
            if not row or not row[0]:
                row = _um_one(c, native,
                              "SELECT feishu_open_id FROM unified_user_mapping "
                              "WHERE github_login=? AND app_id=?", (login, app_id))
        else:
            row = _um_one(c, native,
                          "SELECT feishu_open_id FROM unified_user_mapping "
                          "WHERE gitcode_user=?", (login,))
            if not row or not row[0]:
                row = _um_one(c, native,
                              "SELECT feishu_open_id FROM unified_user_mapping "
                              "WHERE github_login=?", (login,))
        if row and row[0]:
            return row[0]
        try:
            ga = _um_one(c, native,
                         "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) "
                         "AND status=1 LIMIT 1", (login,))
            if ga:
                if app_id:
                    row = _um_one(c, native,
                                  "SELECT feishu_open_id FROM unified_user_mapping "
                                  "WHERE user_id=? AND app_id=?", (ga[0], app_id))
                else:
                    row = _um_one(c, native,
                                  "SELECT feishu_open_id FROM unified_user_mapping "
                                  "WHERE user_id=?", (ga[0],))
                return row[0] if row else None
        except Exception as exc:  # noqa: BLE001 - grape_accounts 可能不存在（原实现同样吞掉）
            log.debug(f"resolve_feishu_open_id: grape_accounts fallback skipped: {exc}")
        return None


def list_user_mappings(conn: Any = None) -> list[tuple]:
    """全部已知映射 (gitcode_user, github_login, feishu_open_id, feishu_name, gitcode_nickname)。

    只取 gitcode_user / github_login 至少一个非空的行（原 get_feishu_mapping 的 where 口径）。
    """
    with _um_cursor(conn) as (c, native):
        c.execute(_um_sql(
            "SELECT gitcode_user, github_login, feishu_open_id, feishu_name, gitcode_nickname "
            "FROM unified_user_mapping WHERE gitcode_user!='' OR github_login!=''", native))
        return [tuple(r) for r in (c.fetchall() or [])]


def list_feishu_name_open_id_pairs(conn: Any = None) -> list[tuple]:
    """群成员 (feishu_name, feishu_open_id)：仅 feishu_name 非空行。"""
    with _um_cursor(conn) as (c, native):
        c.execute(_um_sql(
            "SELECT feishu_name, feishu_open_id FROM unified_user_mapping "
            "WHERE feishu_name!=''", native))
        return [tuple(r) for r in (c.fetchall() or [])]


def bind_feishu_mapping_row(user_id: Any, open_id: str, feishu_name: str, app_id: str,
                            updated_at: str, conn: Any = None) -> dict[str, Any]:
    """FR-18.7 注册名语境绑定飞书：建行（INSERT OR IGNORE）+ 只写飞书三列。

    **不提交**：单库拓扑下提交/回滚归调用方（原 session_manager.bind_feishu_identity
    在同一 db_lock 内两条语句后 commit）；conn=None 时走 mgmt 自有连接（autocommit）。
    """
    with _um_cursor(conn) as (c, native):
        c.execute(_um_sql("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", native),
                  (user_id,))
        created = int(c.rowcount or 0)
        c.execute(_um_sql(
            "UPDATE unified_user_mapping SET feishu_open_id=?, feishu_name=?, app_id=?, "
            "updated_at=? WHERE user_id=?", native),
            (open_id, feishu_name, app_id, updated_at, user_id))
        return {"created": created, "changed": int(c.rowcount or 0)}


def sync_user_mapping(gitcode_user: str, open_id: str = "", feishu_name: str = "",
                      gitcode_nickname: str = "", app_id: str = "", user_id: Any = 0,
                      updated_at: str = "", conn: Any = None) -> dict[str, Any]:
    """按判定规则写 unified_user_mapping 整行（与原 update_unified_user_info 逐句等价）。

    判定（feishu-app-scoped-openid-fix.md §2.2，\"清空\"由 app_id 变化触发，而非 open_id 为空）：
      - app_id 非空 且 != 存储 app_id → 刷新 app_id，清空 open_id/feishu_name（换 app 需重确认）；
      - open_id 非空 → 写 open_id + feishu_name（有则写）+ app_id（有则写）；
      - 否则（open_id 空且 app 未变，如 OAuth 登录）→ 仅刷 nickname/updated_at，保留 open_id/app_id。
    写入：有真实 user_id → INSERT OR IGNORE 建行 + CASE 守卫更新（gitcode_user 空串不覆盖真值）；
    无 user_id 但有既有行 → 按既有 user_id 更新（不动 gitcode_user）；两者皆无 → skipped。
    返回 {"action": "updated"|"skipped", "user_id": ..., "created": n, "changed": n}。**不提交**。
    """
    with _um_cursor(conn) as (c, native):
        stored = _um_one(c, native,
                         "SELECT user_id, feishu_open_id, feishu_name, app_id "
                         "FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))
        stored_open = (stored[1] or "") if stored else ""
        stored_name = (stored[2] or "") if stored else ""
        stored_app = (stored[3] or "") if stored else ""
        app_changed = bool(app_id) and app_id != stored_app
        if open_id:
            final_app = app_id or stored_app
        elif app_changed:
            open_id = ""
            feishu_name = ""
            final_app = app_id
        else:
            open_id = stored_open
            if not feishu_name:
                feishu_name = stored_name
            final_app = stored_app
        created = 0
        if user_id:
            c.execute(_um_sql(
                "INSERT OR IGNORE INTO unified_user_mapping (user_id, updated_at) "
                "VALUES (?, datetime('now'))", native), (user_id,))
            created = int(c.rowcount or 0)
            c.execute(_um_sql(
                "UPDATE unified_user_mapping SET gitcode_user=CASE WHEN ?='' THEN gitcode_user "
                "ELSE ? END, gitcode_nickname=?, feishu_open_id=?, feishu_name=?, app_id=?, "
                "updated_at=? WHERE user_id=?", native),
                (gitcode_user, gitcode_user, gitcode_nickname, open_id, feishu_name,
                 final_app, updated_at, user_id))
        elif stored:
            c.execute(_um_sql(
                "UPDATE unified_user_mapping SET gitcode_nickname=?, feishu_open_id=?, "
                "feishu_name=?, app_id=?, updated_at=? WHERE user_id=?", native),
                (gitcode_nickname, open_id, feishu_name, final_app, updated_at, stored[0]))
        else:
            # 无 user_id 也无既有记录：跳过（user_id 必须为真实 grape_accounts.id）
            return {"action": "skipped", "user_id": 0, "created": 0, "changed": 0}
        return {"action": "updated", "user_id": user_id or stored[0], "created": created,
                "changed": int(c.rowcount or 0)}


# ---------- unified_user_mapping 本地库引导（DDL + 兼容列迁移 + 回填） ----------
# 表归 mgmt 域，schema 引导语句随之归位；kernel/api 进程在**单库/视图期**启动时仍需
# 保证本库有该表，故按「传入调用方连接」的方式复用同一份 DDL（不接受拼接 SQL）。
UNIFIED_USER_MAPPING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS unified_user_mapping (
    user_id INTEGER PRIMARY KEY,
    gitcode_user TEXT DEFAULT '',
    gitcode_nickname TEXT DEFAULT '',
    feishu_open_id TEXT DEFAULT '',
    feishu_name TEXT DEFAULT '',
    app_id TEXT DEFAULT '',               -- ★ 产生 feishu_open_id 的飞书 App（open_id 为 app 级，见 feishu-app-scoped-openid-fix.md）
    github_login TEXT DEFAULT '',
    dingtalk_user_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
)
"""



def unified_user_mapping_ddl() -> str:
    """unified_user_mapping 建表 DDL（kernel 初始化与单测复用；常量本身不对外暴露）。"""
    return UNIFIED_USER_MAPPING_TABLE_SQL

def ensure_user_mapping_schema(conn: Any) -> None:
    """在调用方连接上引导 unified_user_mapping（与原 session_manager 启动块逐步骤等价）。

    步骤与容错口径（每步独立 try/except，仅 CREATE 允许抛出——原实现同样在启动时直接抛）：
      1) 结构不匹配（缺 gitcode_nickname/github_login/dingtalk_user_id）→ DROP 后重建；
      2) CREATE TABLE IF NOT EXISTS（UNIFIED_USER_MAPPING_TABLE_SQL）；
      3) 兼容列 gitcode_id（Issue #211）；
      4) 回填：grape_accounts 已有账号建空行 + external_identities 回填平台列真值（只填空值）；
      5) 兼容列 app_id（feishu-app-scoped-openid-fix.md）。
    幂等：可每次启动执行。conn 必填（DDL 必须落在调用方所在库，不能走跨域连接）。
    """
    if conn is None:
        raise ValueError("ensure_user_mapping_schema 需要调用方连接（本地库引导）")
    # 1) 彻底重建：结构不匹配新 DDL 则 DROP 后重建（历史遗留表为空，直接 DROP 无数据丢失）
    try:
        _um_cols = [r[1] for r in conn.execute("PRAGMA table_info(unified_user_mapping)").fetchall()]
        if _um_cols and not {"gitcode_nickname", "github_login", "dingtalk_user_id"}.issubset(set(_um_cols)):
            conn.execute("DROP TABLE unified_user_mapping")
            conn.commit()
            log.info("Rebuilt unified_user_mapping (structure mismatch -> dropped & recreated)")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Rebuild unified_user_mapping check failed: {e}")
    # 2) 建表（失败必须抛出：原实现在启动时同样直接抛）
    conn.execute(UNIFIED_USER_MAPPING_TABLE_SQL)
    # 3) 兼容列 gitcode_id（Issue #211，非 OAuth 注册用户的关联字段）
    try:
        _um_cols_gid = [r[1] for r in conn.execute("PRAGMA table_info(unified_user_mapping)").fetchall()]
        if "gitcode_id" not in _um_cols_gid:
            conn.execute("ALTER TABLE unified_user_mapping ADD COLUMN gitcode_id TEXT DEFAULT ''")
            conn.commit()
            log.info("Added unified_user_mapping.gitcode_id column")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Add unified_user_mapping.gitcode_id failed: {e}")
    # 4) 回填：grape_accounts 已有但未映射的用户（INSERT OR IGNORE 不覆盖已有飞书绑定），
    #    使 /api/tasks/notify 能解析 assignee→user_id；幂等，每次启动执行
    try:
        try:
            _has_ga = conn.execute("SELECT 1 FROM grape_accounts LIMIT 1").fetchone()
        except Exception:
            _has_ga = None  # 表不存在
        if _has_ga:
            # FR-18.4: backfill 只建 (user_id) 空行，不灌注册名
            conn.execute(
                "INSERT OR IGNORE INTO unified_user_mapping (user_id) "
                "SELECT id FROM grape_accounts WHERE id IS NOT NULL AND id NOT IN ('', '0')")
            # FR-18.4: 从 external_identities 回填平台列真值（仅填空值不覆盖 PAT 已写真值）
            try:
                conn.execute(
                    "UPDATE unified_user_mapping SET gitcode_user="
                    "(SELECT ei.external_login FROM external_identities ei "
                    "WHERE ei.grape_user_id=unified_user_mapping.user_id "
                    "AND ei.provider='gitcode' AND ei.status=1 AND ei.external_login!='' LIMIT 1) "
                    "WHERE (gitcode_user IS NULL OR gitcode_user='')")
                conn.execute(
                    "UPDATE unified_user_mapping SET github_login="
                    "(SELECT ei.external_login FROM external_identities ei "
                    "WHERE ei.grape_user_id=unified_user_mapping.user_id "
                    "AND ei.provider='github' AND ei.status=1 AND ei.external_login!='' LIMIT 1) "
                    "WHERE (github_login IS NULL OR github_login='')")
            except Exception as exc:  # noqa: BLE001 - 列不存在时跳过，但不再静默
                log.warning(f"backfill github_login failed: {exc}")
            # 补充 gitcode_id 关联：从 external_identities 读
            try:
                conn.execute(
                    "UPDATE unified_user_mapping SET gitcode_id = "
                    "(SELECT ei.external_id FROM external_identities ei "
                    "WHERE ei.grape_user_id = unified_user_mapping.user_id AND ei.provider='gitcode') "
                    "WHERE gitcode_id='' AND user_id IN "
                    "(SELECT grape_user_id FROM external_identities WHERE provider='gitcode')")
            except Exception as exc:  # noqa: BLE001 - external_identities 表可能不存在
                log.warning(f"backfill gitcode_id failed: {exc}")
            conn.commit()
        else:
            log.info("grape_accounts table not yet created, skipping backfill")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Backfill unified_user_mapping failed: {e}")
    # 5) 兼容列 app_id（feishu-app-scoped-openid-fix.md，open_id 为 app 级）
    try:
        _um_cols2 = [r[1] for r in conn.execute("PRAGMA table_info(unified_user_mapping)").fetchall()]
        if "app_id" not in _um_cols2:
            conn.execute("ALTER TABLE unified_user_mapping ADD COLUMN app_id TEXT DEFAULT ''")
            conn.commit()
            log.info("Added unified_user_mapping.app_id column")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Add unified_user_mapping.app_id failed: {e}")

# ── 渠道安装表（grape_t_channel_installation 归 mgmt）────────────────────────
# runtime/im 需要读写这张表；SQL 文本只放在本模块一份，调用方传自己的连接进来
# （连接仍归调用方管事务/关闭，语义零变化）。

def installation_active_row(conn, scope: str, key: str, channel_type: str = "") -> tuple | None:
    """按轨 + 归属 key 取最新一条活跃安装。"""
    sql = ("SELECT id, channel_type, scope, owner_user_id, repo_key, config "
           "FROM grape_t_channel_installation WHERE status='active' AND scope=? "
           "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)")
    params: list = [scope, key, key]
    if channel_type:
        sql += " AND channel_type=?"
        params.append(channel_type)
    return conn.execute(sql + " ORDER BY updated_at DESC LIMIT 1", tuple(params)).fetchone()


def installation_config_by_app(conn, app_id: str, scope: str, key: str) -> str | None:
    """按 legacy_app_id 取该轨安装的 config 原文。"""
    row = conn.execute(
        "SELECT config FROM grape_t_channel_installation WHERE legacy_app_id=? AND scope=? "
        "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
        (app_id, scope, key, key)).fetchone()
    return row[0] if row else None


def installation_activate(conn, app_id: str, scope: str, key: str) -> None:
    """把该 app 的安装行置 active（换 app 复用同一行）。"""
    conn.execute(
        "UPDATE grape_t_channel_installation SET status='active', updated_at=datetime('now') "
        "WHERE legacy_app_id=? AND scope=? "
        "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
        (app_id, scope, key, key))


def installation_revoke(conn, channel_type: str, scope: str, app_id: str, key: str) -> int:
    """解绑：置 revoked（不硬删），返回受影响行数。"""
    cur = conn.execute(
        "UPDATE grape_t_channel_installation SET status='revoked', updated_at=datetime('now') "
        "WHERE channel_type=? AND scope=? AND legacy_app_id=? "
        "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
        (channel_type, scope, app_id, key, key))
    return int(getattr(cur, "rowcount", 0) or 0)


def installation_active_configs(conn) -> list[tuple]:
    """所有活跃安装的 (id, config)，供运行时状态就地更新。"""
    return conn.execute(
        "SELECT id, config FROM grape_t_channel_installation WHERE status='active'").fetchall()


def installation_set_config(conn, cfg_json: str, row_id) -> None:
    """就地写回某行 config。"""
    conn.execute("UPDATE grape_t_channel_installation SET config=? WHERE id=?", (cfg_json, row_id))


def installation_active_installs(conn) -> list[tuple]:
    """所有活跃安装的完整六列（按 config.app_id 反查安装用）。"""
    return conn.execute(
        "SELECT id, scope, owner_user_id, repo_key, config, status "
        "FROM grape_t_channel_installation WHERE status='active'").fetchall()



# ---------- 任意拓扑原语（自有 DSN 走 db_registry；单库/测试拓扑回落进程主库）----------
#
# runtime / insight 进程要读写在 mgmt 库的表（渠道租约、动作过滤器）时，SQL 只在本域留一份，
# 连接按拓扑自适应：有自有 DSN → mgmt 连接；没有（单库 / 视图期 / 测试）→ 进程主库 + db_lock。
# 方言一律可移植（`?` 占位符 / `datetime('now')` / `IFNULL`），两条连接都能执行。


@contextmanager
def _any_cursor(write: bool = False):
    """mgmt 表游标：自有 DSN 优先（autocommit），否则回落进程主库（持有 db_lock）。"""
    if db_registry.uses_own_dsn("mgmt"):
        with db_registry.cursor("mgmt") as c:
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
    if db_registry.uses_own_dsn("mgmt"):
        with db_registry.transaction("mgmt") as c:
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
                log.debug(f"mgmt public rollback skipped: {exc}")
            raise


# ---------- 渠道租约（grape_t_channel_lease 归 mgmt；runtime 的 IM 主从选举用） ----------

def acquire_channel_lease(lease_key: str, token: str, owner: str = "",
                          ttl_seconds: int = 60) -> bool:
    """抢占渠道租约（CAS + 过期回收），可重放。

    条件：租约为空、已过期，或本来就属于自己（同 token 重入）。
    行不存在时插入（并发插入由 lease_key 主键保证只有一方成功）。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    stamp = now.isoformat()
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE grape_t_channel_lease SET lease_token=?, lease_owner=?, "
            "lease_expires_at=?, lease_acquired_at=IFNULL(lease_acquired_at, ?), updated_at=? "
            "WHERE lease_key=? AND (lease_token IS NULL OR lease_token=? "
            "OR lease_expires_at IS NULL OR lease_expires_at < ?)",
            (token, owner, expires, stamp, stamp, lease_key, token, stamp))
        if int(getattr(cur, "rowcount", 0) or 0) > 0:
            return True
        try:
            c.execute(
                "INSERT INTO grape_t_channel_lease "
                "(lease_key, channel_type, lease_token, lease_owner, lease_expires_at, "
                "lease_acquired_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (lease_key, lease_key.split(":")[0] if ":" in lease_key else "", token,
                 owner, expires, stamp, stamp))
            return True
        except Exception:  # noqa: BLE001 - 并发抢占时主键冲突即为失败
            return False


def release_channel_lease(lease_key: str, token: str) -> bool:
    """释放租约：仅当 token 匹配时清空（幂等：已释放则返回 False）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE grape_t_channel_lease SET lease_token=NULL, lease_owner=NULL, "
            "lease_expires_at=NULL, updated_at=datetime('now') "
            "WHERE lease_key=? AND lease_token=?", (lease_key, token))
        return int(getattr(cur, "rowcount", 0) or 0) > 0


def renew_channel_lease(lease_key: str, token: str, owner: str = "", ttl_seconds: int = 60,
                        expected_token: str | None = None) -> bool:
    """IM 渠道租约续约（CAS 语义，可重放）。

    仅当 `lease_token` 为空或等于 `expected_token` 时才写入，避免抢掉别人的租约。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    stamp = now.isoformat()
    with _any_cursor(write=True) as c:
        if expected_token:
            cur = c.execute(
                "UPDATE grape_t_channel_lease SET lease_token=?, lease_owner=?, "
                "lease_expires_at=?, lease_acquired_at=IFNULL(lease_acquired_at, ?), updated_at=? "
                "WHERE lease_key=? AND (lease_token IS NULL OR lease_token=?)",
                (token, owner, expires, stamp, stamp, lease_key, expected_token))
        else:
            cur = c.execute(
                "UPDATE grape_t_channel_lease SET lease_token=?, lease_owner=?, "
                "lease_expires_at=?, lease_acquired_at=IFNULL(lease_acquired_at, ?), updated_at=? "
                "WHERE lease_key=? AND lease_token IS NULL",
                (token, owner, expires, stamp, stamp, lease_key))
        return int(getattr(cur, "rowcount", 0) or 0) > 0


# ---------- 事件动作过滤器（event_action_filter 归 mgmt） ----------

def add_event_action_filter_row(fields: dict, created_at: str) -> int:
    """新增动作过滤器行（列名 dynamic 由本域白名单收敛）；返回自增 id。"""
    data = dict(fields or {})
    data["created_at"] = created_at
    cols = list(data.keys())
    ph = ", ".join(["?"] * len(cols))
    with _any_cursor(write=True) as c:
        cur = c.execute(f"INSERT INTO event_action_filter ({', '.join(cols)}) VALUES ({ph})",
                        tuple(data[k] for k in cols))
        return int(getattr(cur, "lastrowid", 0) or 0)


def update_event_action_filter_row(rule_id: int, fields: dict, updated_at: str) -> int:
    """按 id 更新动作过滤器行（空 fields 直接返回 0）。"""
    data = dict(fields or {})
    if not data:
        return 0
    data["updated_at"] = updated_at
    sets = ", ".join(f"{k}=?" for k in data)
    params = list(data.values()) + [int(rule_id)]
    with _any_cursor(write=True) as c:
        cur = c.execute(f"UPDATE event_action_filter SET {sets} WHERE id=?", params)
        return int(getattr(cur, "rowcount", 0) or 0)


def delete_event_action_filter_row(rule_id: int) -> int:
    with _any_cursor(write=True) as c:
        cur = c.execute("DELETE FROM event_action_filter WHERE id=?", (int(rule_id),))
        return int(getattr(cur, "rowcount", 0) or 0)


# ---------- 平台身份 → 账号（runtime 侧读 mgmt 身份的替身；单库回落进程库） ----------

def mapped_user_id(login: str, um_column: str = "gitcode_user") -> Any:
    """按平台登录名解析有效账号 id（unified_user_mapping → grape_accounts.status=1 校验）。

    无命中返回 0（与原 issue 建档 SQL 的 COALESCE(...,0) 口径一致）。
    """
    if not login:
        return 0
    col = "github_login" if um_column == "github_login" else "gitcode_user"
    sql = (f"SELECT m.user_id FROM unified_user_mapping m WHERE m.{col}=? "
           "AND m.user_id IS NOT NULL AND m.user_id NOT IN ('', '0') "
           "AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=m.user_id AND ga.status=1) "
           "ORDER BY m.user_id LIMIT 1")
    with _any_cursor() as c:
        c.execute(sql, (login,))
        row = c.fetchone()
    return row[0] if row else 0


def external_user_id(provider: str, external_id: str) -> Any:
    """按 (provider, external_id) 取账号 id（external_identities 归 mgmt）；无命中返回 None。"""
    if not provider or external_id in (None, ""):
        return None
    with _any_cursor() as c:
        c.execute("SELECT ei.grape_user_id FROM external_identities ei "
                  "WHERE ei.provider=? AND ei.status=1 AND ei.external_id=? LIMIT 1",
                  (provider, str(external_id)))
        row = c.fetchone()
    return row[0] if row else None


# ---------- 迁移期批量回填（跨域 JOIN 的替代：先取本域映射，再交给各域回填） ----------

def repo_org_map() -> dict[str, str]:
    """有归属组织的仓库映射 {repo_key: org_id}（org_id 为空的行不返回）。"""
    with _any_cursor() as c:
        c.execute("SELECT repo_key, org_id FROM managed_repos WHERE org_id <> ''")
        return {r[0]: r[1] for r in (c.fetchall() or []) if r[0] and r[1]}


def skill_category_map() -> dict[str, int]:
    """技能分类 {slug: id}（skill_category 归本域）。"""
    with _any_cursor() as c:
        c.execute("SELECT slug, id FROM skill_category WHERE slug IS NOT NULL AND slug != ''")
        return {r[0]: int(r[1]) for r in (c.fetchall() or []) if r[0]}


#: 跟随 repo_key 的 mgmt 表（固定白名单：不接受调用方传表名）
_ORG_BACKFILL_TABLES = ("devops_pipeline_scene", "agent_shared_knowledge")


def backfill_org_id_by_repo(mapping: dict) -> dict[str, int]:
    """按 repo_key→org_id 映射回填本域表的空 org_id（逐仓库 UPDATE，幂等）。

    替代原来的 `UPDATE <表> SET org_id=(SELECT ... FROM managed_repos ...) WHERE repo_key IN (...)`
    跨域关联子查询：映射由本域取，回填落在各自 owner 库。
    """
    out: dict[str, int] = {}
    if not mapping:
        return out
    with _any_tx() as c:
        for tbl in _ORG_BACKFILL_TABLES:
            n = 0
            for rk, org in mapping.items():
                cur = c.execute(
                    f"UPDATE {tbl} SET org_id=? WHERE repo_key=? AND (org_id='' OR org_id IS NULL)",
                    (org, rk))
                n += int(getattr(cur, "rowcount", 0) or 0)
            out[tbl] = n
    return out


# ---------- 巡检（api/patrol.py）：仓库归属 + findings 落库（表归 mgmt） ----------

def managed_repo_host_user(repo_key: str) -> str:
    """仓库的 host_user_id（无行/空值返回空串）。"""
    with _any_cursor() as c:
        c.execute("SELECT host_user_id FROM managed_repos WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
    return str(row[0]) if row and row[0] else ""


def repo_admin_user_id(repo_key: str):
    """仓库的首个管理员 user_id（按 created_at 最早）；无则返回 0。"""
    with _any_cursor() as c:
        c.execute("SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 "
                  "ORDER BY created_at LIMIT 1", (repo_key,))
        row = c.fetchone()
    return row[0] if row and row[0] else 0


def patrol_findings_known() -> set:
    """已落库的 (kind, task_id) 集合（audit 重扫的短路集）。"""
    with _any_cursor() as c:
        c.execute("SELECT kind, task_id FROM grape_t_patrol_findings WHERE task_id != ''")
        return {(r[0], r[1]) for r in (c.fetchall() or [])}


_PATROL_FINDING_INSERT_SQL = (
    "INSERT OR IGNORE INTO grape_t_patrol_findings "
    "(id, kind, task_id, event_seq, repo_key, issue_id, owner, detail, suggestion, "
    "status, action_meta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def patrol_findings_insert_many(rows: list, chunk: int = 500) -> int:
    """攒批 executemany + 每块单次提交；单块失败吞掉其余块照常（返回真实插入数）。"""
    inserted = 0
    if not rows:
        return 0
    with _any_cursor(write=True) as c:
        for i in range(0, len(rows), chunk):
            try:
                c.executemany(_PATROL_FINDING_INSERT_SQL, rows[i:i + chunk])
                inserted += int(getattr(c, "rowcount", 0) or 0)
            except Exception as exc:  # noqa: BLE001 - S5 口径：单块丢失，其余照常
                log.warning(f"patrol findings batch insert failed (chunk {i // chunk}): {exc}")
    return inserted


def patrol_findings_insert_one(row: tuple) -> bool:
    """插一条 finding（UNIQUE + OR IGNORE 幂等）；返回是否真插入。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(_PATROL_FINDING_INSERT_SQL, row)
        return int(getattr(cur, "rowcount", 0) or 0) == 1


def patrol_findings_delete_terminal(statuses, cutoff_iso: str) -> int:
    """按保留期清理终态 finding（status IN 列表 + updated_at 早于 cutoff）。"""
    _statuses = tuple(statuses or ())
    if not _statuses:
        return 0
    _ph = ",".join(["?"] * len(_statuses))
    with _any_cursor(write=True) as c:
        cur = c.execute(
            f"DELETE FROM grape_t_patrol_findings WHERE status IN ({_ph}) "
            "AND updated_at != '' AND updated_at < ?", (*_statuses, cutoff_iso))
        return int(getattr(cur, "rowcount", 0) or 0)


# ---------- 看板/报表读（insight dashboard 用；表归 mgmt） ----------

def account_names_by_ids(ids) -> dict:
    """批量取账号显示名 {id: username}（分块，500/批）。"""
    out: dict = {}
    todo = [str(u) for u in (ids or []) if u]
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        ph = ",".join(["?"] * len(part))
        with _any_cursor() as c:
            c.execute(f"SELECT id, username FROM grape_accounts WHERE id IN ({ph})", part)
            for uid, name in (c.fetchall() or []):
                if name:
                    out[uid] = name
    return out


def repo_key_in_candidates(repo_keys) -> str:
    """在候选里找出真实存在的 managed_repos.repo_key（无命中返回空串）。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return ""
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({ph}) LIMIT 1", keys)
        row = c.fetchone()
    return (row[0] if row else "") or ""


def pipeline_scene_advanced_options(scene_id, repo_keys) -> str:
    """场景的 advanced_options 原文（JSON 串，解析归调用方）；无行返回空串。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not scene_id or not keys:
        return ""
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(
            "SELECT advanced_options FROM devops_pipeline_scene "
            f"WHERE scene_id=? AND repo_key IN ({ph}) AND is_active=1 LIMIT 1",
            [scene_id] + keys)
        row = c.fetchone()
    return (row[0] if row and row[0] else "") or ""


# ---------- API 令牌表（session_api_tokens / api_tokens 归 mgmt；auth_sdk 在 kernel 层调用） ----------
#
# 为什么在 mgmt：两张表在三库拓扑里落在 mgmt 库，而签发/校验逻辑是 kernel 的共享能力，
# 由 api / mgmt / runtime 三个进程共同调用 —— 跨域时连接必须走本域（自有 DSN 或单库回落）。

def session_token_insert(session_key: str, repo_key: str, agent_type: str, user_id,
                         token_sha256: str, expires_at: int, kind: str | None = None) -> int:
    """插入一条会话 token（kind=None 为 worker/tk_，'takeover' 为接管）；返回自增 id。"""
    with _any_cursor(write=True) as c:
        if kind:
            cur = c.execute(
                "INSERT INTO session_api_tokens "
                "(session_key, repo_key, agent_type, user_id, token_sha256, expires_at, kind) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_key, repo_key, agent_type, user_id, token_sha256, int(expires_at), kind))
        else:
            cur = c.execute(
                "INSERT INTO session_api_tokens "
                "(session_key, repo_key, agent_type, user_id, token_sha256, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_key, repo_key, agent_type, user_id, token_sha256, int(expires_at)))
        return int(getattr(cur, "lastrowid", 0) or 0)


def session_token_by_sha(token_sha256: str, kind: str | None = None) -> tuple | None:
    """按 sha256 取会话 token 行 (id, session_key, repo_key, agent_type, user_id, expires_at)。

    kind 非空时追加 `kind=?` 过滤（worker 的 tk_ 与接管的 tko_ 互不通用）。
    """
    with _any_cursor() as c:
        if kind:
            c.execute(
                "SELECT id, session_key, repo_key, agent_type, user_id, expires_at "
                "FROM session_api_tokens WHERE token_sha256=? AND kind=?", (token_sha256, kind))
        else:
            c.execute(
                "SELECT id, session_key, repo_key, agent_type, user_id, expires_at "
                "FROM session_api_tokens WHERE token_sha256=?", (token_sha256,))
        return c.fetchone()


def session_token_set_expiry(session_key: str, expires_at: int) -> int:
    """把某会话的全部 token 有效期改写（延迟吊销用）；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("UPDATE session_api_tokens SET expires_at=? WHERE session_key=?",
                        (int(expires_at), session_key))
        return int(getattr(cur, "rowcount", 0) or 0)


def session_token_shrink_active(session_key: str, expires_at: int, now: int) -> int:
    """只提前作废仍有效的旧 token（已过期的保持原样，交给 purge）；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE session_api_tokens SET expires_at=? "
            "WHERE session_key=? AND CAST(expires_at AS INTEGER) > ?",
            (int(expires_at), session_key, int(now)))
        return int(getattr(cur, "rowcount", 0) or 0)


def session_token_delete_expired(now: int) -> int:
    """清理过期会话 token；返回删除行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("DELETE FROM session_api_tokens WHERE CAST(expires_at AS INTEGER) <= ?",
                        (int(now),))
        return int(getattr(cur, "rowcount", 0) or 0)


def session_token_delete_by_session(session_key: str, kind: str | None = None) -> int:
    """按 session_key（可选 kind）立即删除（接管 token 的 finish/abort）；返回删除行数。"""
    with _any_cursor(write=True) as c:
        if kind:
            cur = c.execute("DELETE FROM session_api_tokens WHERE session_key=? AND kind=?",
                            (session_key, kind))
        else:
            cur = c.execute("DELETE FROM session_api_tokens WHERE session_key=?", (session_key,))
        return int(getattr(cur, "rowcount", 0) or 0)


def session_token_renew(session_key: str, kind: str, expires_at: int) -> int:
    """续期（仅当当前有效期早于目标值）；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE session_api_tokens SET expires_at=? "
            "WHERE session_key=? AND kind=? AND expires_at < ?",
            (int(expires_at), session_key, kind, int(expires_at)))
        return int(getattr(cur, "rowcount", 0) or 0)


def session_token_rotate(session_key: str, repo_key: str, agent_type: str, user_id,
                         token_sha256: str, expires_at: int, now: int) -> None:
    """轮换：作废仍有效的旧 token + 插入新 token（**同一事务**）。

    注意跨域边界：调用方若还要回写别的域（如 agent_tasks.env_extra）的快照，
    那一步已不在本事务内 —— 顺序上必须先完成本原语（新 token 已落库）再回写快照，
    并由调用方自己的实例锁串行化，失败重试即可收敛。
    """
    with _any_tx() as c:
        c.execute("UPDATE session_api_tokens SET expires_at=? "
                  "WHERE session_key=? AND CAST(expires_at AS INTEGER) > ?",
                  (int(now), session_key, int(now)))
        c.execute("INSERT INTO session_api_tokens "
                  "(session_key, repo_key, agent_type, user_id, token_sha256, expires_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (session_key, repo_key, agent_type, user_id, token_sha256, int(expires_at)))


def api_token_insert(user_id, token_sha256: str, name: str, created_at: str) -> int:
    """插入用户级 API Key（只存 sha256）；返回自增 id。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "INSERT INTO api_tokens (user_id, token_sha256, name, created_at) VALUES (?,?,?,?)",
            (user_id, token_sha256, name, created_at))
        return int(getattr(cur, "lastrowid", 0) or 0)


def api_token_by_sha(token_sha256: str) -> tuple | None:
    """按 sha256 取 (id, user_id, name, scopes, revoked)。"""
    with _any_cursor() as c:
        c.execute("SELECT id, user_id, name, scopes, revoked FROM api_tokens "
                  "WHERE token_sha256=?", (token_sha256,))
        return c.fetchone()


def api_token_touch(token_id, last_used_at: str) -> int:
    """更新最近使用时间（失败由调用方吞掉，不影响校验结果）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                        (str(last_used_at), token_id))
        return int(getattr(cur, "rowcount", 0) or 0)


def api_token_revoke(token_id, user_id) -> int:
    """软吊销（revoked=1，保审计）；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=?",
                        (token_id, user_id))
        return int(getattr(cur, "rowcount", 0) or 0)


def api_token_list(user_id) -> list[tuple]:
    """未吊销的 API Key 列表 (id, name, scopes, created_at, last_used_at)，新的在前。"""
    with _any_cursor() as c:
        c.execute("SELECT id, name, scopes, created_at, last_used_at FROM api_tokens "
                  "WHERE user_id=? AND revoked=0 ORDER BY id DESC", (user_id,))
        return [tuple(r) for r in (c.fetchall() or [])]


# ---------- 仓库改名迁移（mgmt 段：托管仓库主键重建 + 用户仓库 + 本域 repo_key 表） ----------

#: 以 repo_key 为索引的 mgmt 表（固定白名单，不接受调用方传表名）
REPO_KEY_TABLES = ("event_route_config", "agent_shared_knowledge",
                   "devops_pipeline_scene", "pipeline_instances")


def _mg_existing_tables(c) -> set:
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        try:
            return {list(r)[-1] for r in c.execute("SHOW TABLES")}
        except Exception:
            return set()


def _mg_has_repo_key(c, table: str) -> bool:
    try:
        return "repo_key" in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return False


def _mg_unique_repo_key(c, table: str) -> bool:
    try:
        for ci in c.execute(f"PRAGMA table_info({table})"):
            if ci[5] == 1 and ci[1] == "repo_key":
                return True
        for idx in c.execute(f"PRAGMA index_list({table})"):
            if idx[2] == 1:
                cols = [x[2] for x in c.execute(f"PRAGMA index_info({idx[1]})")]
                if cols and cols[0] == "repo_key":
                    return True
    except Exception as exc:  # noqa: BLE001 - 探测失败按「无唯一约束」处理（两个分支都要能跑）
        log.debug(f"unique-repo-key probe skipped for {table}: {exc}")
    return False


def _mg_migrate_managed_repos(c, old: str, new: str, new_owner: str, new_repo: str) -> int:
    """managed_repos：repo_key 是主键 → DELETE 旧行 + INSERT 新行，保留全部托管字段。"""
    c.execute(
        "SELECT repo_owner, repo_name, full_name, webhook_id, dev_scene, host_status, "
        "bot_gitcode_token, gitcode_webhook_token, bot_username, github_webhook_token, "
        "bot_github_token, org_id, host_user_id, webhook_secret, created_at "
        "FROM managed_repos WHERE repo_key=?", (old,))
    row = c.fetchone()
    if not row:
        return 0
    (repo_owner, repo_name, full_name, webhook_id, dev_scene, host_status,
     bot_gc, wh_gc, bot_user, wh_gh, bot_gh, org_id, host_uid, wh_secret, created_at) = row
    new_full = f"{new_owner}/{new_repo}"
    c.execute("DELETE FROM managed_repos WHERE repo_key=?", (old,))
    try:
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, host_status, bot_gitcode_token, gitcode_webhook_token, "
            "bot_username, github_webhook_token, bot_github_token, org_id, host_user_id, "
            "webhook_secret, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new, new_owner, new_repo, new_full, webhook_id, dev_scene or "", host_status or "",
             bot_gc or "", wh_gc or "", bot_user or "", wh_gh or "", bot_gh or "",
             org_id or "", host_uid or 0, wh_secret or "", created_at))
    except Exception:
        # 列不全时降级（兼容未迁移的库）
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username, "
            "org_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new, new_owner, new_repo, new_full, webhook_id, dev_scene or "",
             bot_gc or "", wh_gc or "", bot_user or "", org_id or ""))
    return 1


def _mg_migrate_user_repos(c, old: str, new: str, new_owner: str, new_repo: str) -> int:
    """user_repos：复合主键 (user_id, repo_key)，目标已存在时删旧留新。"""
    c.execute("SELECT user_id, owner, name, description, role, is_admin, is_join, "
              "created_at, synced_at FROM user_repos WHERE repo_key=?", (old,))
    rows = c.fetchall() or []
    if not rows:
        return 0
    cnt = 0
    for (uid, owner, name, desc, role, is_admin, is_join, created_at, synced_at) in rows:
        c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (uid, new))
        try:
            c.execute(
                "INSERT INTO user_repos (user_id, repo_key, owner, name, description, "
                "role, is_admin, is_join, created_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, new, new_owner, new_repo, desc or "", role or "",
                 is_admin or 0, is_join or 0, created_at or "", synced_at or ""))
        except Exception:
            c.execute(
                "INSERT INTO user_repos (user_id, repo_key, owner, name, role, is_admin) "
                "VALUES (?,?,?,?,?,?)",
                (uid, new, new_owner, new_repo, role or "", is_admin or 0))
        cnt += 1
    c.execute("DELETE FROM user_repos WHERE repo_key=?", (old,))
    return cnt


def migrate_repo_key_mgmt(repo_key: str, old_keys, new: str, new_owner: str,
                          new_repo: str) -> dict[str, int]:
    """同一事务内改写本域的仓库数据；返回 {表名: 改写行数}。"""
    keys = [str(k) for k in (old_keys or []) if k]
    out: dict[str, int] = {}
    with _any_tx() as c:
        existing = _mg_existing_tables(c)
        n = _mg_migrate_managed_repos(c, repo_key, new, new_owner, new_repo)
        if n:
            out["managed_repos"] = n
        n = _mg_migrate_user_repos(c, repo_key, new, new_owner, new_repo)
        if n:
            out["user_repos"] = n
        if keys:
            ph = ",".join(["?"] * len(keys))
            for tbl in REPO_KEY_TABLES:
                if tbl not in existing or not _mg_has_repo_key(c, tbl):
                    continue
                m = 0
                if _mg_unique_repo_key(c, tbl):
                    c.execute(f"SELECT repo_key FROM {tbl} WHERE repo_key IN ({ph})", keys)
                    for (rk,) in (c.fetchall() or []):
                        if c.execute(f"SELECT 1 FROM {tbl} WHERE repo_key=? LIMIT 1",
                                     (new,)).fetchone():
                            cur = c.execute(f"DELETE FROM {tbl} WHERE repo_key=?", (rk,))
                        else:
                            cur = c.execute(f"UPDATE {tbl} SET repo_key=? WHERE repo_key=?",
                                            (new, rk))
                        m += int(getattr(cur, "rowcount", 0) or 0)
                else:
                    cur = c.execute(f"UPDATE {tbl} SET repo_key=? WHERE repo_key IN ({ph})",
                                    [new] + keys)
                    m = int(getattr(cur, "rowcount", 0) or 0)
                if m:
                    out[tbl] = m
    return out


# ---------- 仓库级联清理（mgmt 段） ----------

def purge_repo_rows(keys, dry_run: bool = False) -> dict[str, int]:
    """删除/统计仓库在 mgmt 域的数据（知识库 + 引用 + 本域 repo_key 表）。"""
    _keys = [str(k) for k in (keys or []) if k]
    stats: dict[str, int] = {}
    if not _keys:
        return stats
    ph = ",".join(["?"] * len(_keys))
    with _any_tx() as c:
        existing = _mg_existing_tables(c)
        if dry_run:
            for tbl in REPO_KEY_TABLES:
                if tbl in existing:
                    c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE repo_key IN ({ph})", _keys)
                    stats[tbl] = int((c.fetchone() or [0])[0] or 0)
            return stats

        # 1) 依赖子表：scene_knowledge_ref ← agent_shared_knowledge.id
        if "agent_shared_knowledge" in existing and "scene_knowledge_ref" in existing:
            c.execute(f"SELECT id FROM agent_shared_knowledge WHERE repo_key IN ({ph})", _keys)
            ids = [r[0] for r in (c.fetchall() or [])]
            if ids:
                _p = ",".join(["?"] * len(ids))
                cur = c.execute(f"DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ({_p})", ids)
                stats["scene_knowledge_ref"] = int(getattr(cur, "rowcount", 0) or 0)
        # 2) 本域 repo_key 表直删
        for tbl in REPO_KEY_TABLES:
            if tbl not in existing:
                continue
            cur = c.execute(f"DELETE FROM {tbl} WHERE repo_key IN ({ph})", _keys)
            stats[tbl] = int(getattr(cur, "rowcount", 0) or 0)
    return stats


# ---------- 渠道租约续用（bot_lease 的 acquire/续期/批量视角；表归 mgmt） ----------

def lease_acquire_row(lease_key: str, channel_type: str, installation_id: str, token: str,
                     owner: str, expires_at: str, now: str) -> bool:
    """惰性建行 + CAS 抢占（两句同一事务）。

    与 `acquire_channel_lease` 的差别：这里先 `INSERT OR IGNORE` 铺行、再 UPDATE 抢占，
    抢占条件只认「token 为空 / 到期为空 / 已过期」（不含同 token 重入）。
    时钟口径：now/expires_at 都由调用方按 Python UTC 生成，不用数据库时间函数。
    """
    with _any_tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO grape_t_channel_lease "
            "(lease_key, channel_type, installation_id, updated_at) VALUES (?,?,?,?)",
            (lease_key, channel_type, installation_id, now))
        cur = c.execute(
            "UPDATE grape_t_channel_lease "
            "SET lease_token = ?, lease_expires_at = ?, lease_owner = ?, "
            "    lease_acquired_at = ?, updated_at = ? "
            "WHERE lease_key = ? "
            "  AND (lease_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)",
            (token, expires_at, owner, now, now, lease_key, now))
        return int(getattr(cur, "rowcount", 0) or 0) > 0


def lease_renew_expiry(lease_key: str, token: str, expires_at: str, now: str) -> int:
    """续期（只改到期时间，不动 token/owner）；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE grape_t_channel_lease SET lease_expires_at = ?, updated_at = ? "
            "WHERE lease_key = ? AND lease_token = ?",
            (expires_at, now, lease_key, token))
        return int(getattr(cur, "rowcount", 0) or 0)


def lease_renew_all_by_owner(owner: str, expires_at: str, now: str) -> int:
    """某个 owner 持有的全部租约统一续期；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE grape_t_channel_lease SET lease_expires_at = ?, updated_at = ? "
            "WHERE lease_owner = ? AND lease_token IS NOT NULL", (expires_at, now, owner))
        return int(getattr(cur, "rowcount", 0) or 0)


def lease_tokens_by_keys(keys) -> dict:
    """批量取 {lease_key: lease_token}（对账用）。"""
    _keys = [str(k) for k in (keys or []) if k]
    if not _keys:
        return {}
    ph = ",".join(["?"] * len(_keys))
    with _any_cursor() as c:
        c.execute(f"SELECT lease_key, lease_token FROM grape_t_channel_lease "
                  f"WHERE lease_key IN ({ph})", _keys)
        return {r[0]: r[1] for r in (c.fetchall() or [])}


def lease_keys_valid(now: str) -> set:
    """当前仍有效的租约键集合（token 非空且未过期）。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT lease_key FROM grape_t_channel_lease "
            "WHERE lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "  AND lease_expires_at >= ?", (now,))
        return {r[0] for r in (c.fetchall() or [])}


def lease_owner_info(lease_key: str) -> tuple | None:
    """租约当前 (owner, token, expires_at)。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT COALESCE(lease_owner,''), COALESCE(lease_token,''), lease_expires_at "
            "FROM grape_t_channel_lease WHERE lease_key=?", (lease_key,))
        return c.fetchone()


def installation_status(installation_id: str):
    """渠道安装状态（无行返回 None）。"""
    with _any_cursor() as c:
        c.execute("SELECT status FROM grape_t_channel_installation WHERE id=?", (installation_id,))
        row = c.fetchone()
    return row[0] if row else None


def installation_active_rows(cursor: str = "", limit: int = 500) -> list[tuple]:
    """active 安装分页（id, channel_type），按 id 游标推进。"""
    with _any_cursor() as c:
        c.execute("SELECT id, channel_type FROM grape_t_channel_installation "
                  "WHERE status='active' AND id > ? ORDER BY id LIMIT ?", (cursor, int(limit)))
        return [tuple(r) for r in (c.fetchall() or [])]


# ---------- 告警收件人（repo 成员 / 映射全量；表归 mgmt） ----------

def repo_member_user_ids(repo_keys) -> list[int]:
    """仓库成员 user_id 并集（去重）；repo_keys 为空返回空列表。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(f"SELECT DISTINCT user_id FROM user_repos WHERE repo_key IN ({ph})", keys)
        return [int(r[0]) for r in (c.fetchall() or []) if r and r[0]]


def mapped_feishu_user_ids() -> list[int]:
    """绑定了飞书 open_id 的账号 id 全量（收件人兜底）。"""
    with _any_cursor() as c:
        c.execute("SELECT DISTINCT user_id FROM unified_user_mapping"
                  " WHERE feishu_open_id!='' AND user_id IS NOT NULL "
                  "AND user_id NOT IN ('', '0')")
        return [int(r[0]) for r in (c.fetchall() or []) if r and r[0]]


# ---------- 渠道入站去重 / 会话绑定（engine_wiring 的 IM 侧；表归 mgmt） ----------
#
# 与 W2 那批 `db_registry.cursor("mgmt") + %s/NOW()` 原语的区别：这批走 `_any_cursor/_any_tx`，
# 单库/测试拓扑回落进程主库、三库拓扑走 mgmt 自有 DSN，方言一律可移植（`?` + 调用方传时间）。

def inbound_dedup_seen(installation_id: str, message_id: str) -> bool:
    """该入站消息是否已处理过（占位行存在即视为已处理）。"""
    with _any_cursor() as c:
        c.execute("SELECT 1 FROM grape_t_channel_inbound_dedup "
                  "WHERE installation_id=? AND message_id=?", (installation_id, message_id))
        return c.fetchone() is not None


def inbound_dedup_claim(installation_id: str, message_id: str, token: str,
                        now: str = "") -> bool:
    """抢占去重位（首占返回 True）；依赖 (installation_id, message_id) 唯一键幂等。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO grape_t_channel_inbound_dedup "
            "(installation_id, message_id, state, claim_token) VALUES (?,?, 'claimed', ?)",
            (installation_id, message_id, token))
        return int(getattr(cur, "rowcount", 0) or 0) > 0


def inbound_dedup_set_state(installation_id: str, message_id: str, state: str, now: str = "") -> bool:
    """把去重位状态推进（claimed → marked 等）；返回是否命中行。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("UPDATE grape_t_channel_inbound_dedup SET state=? "
                        "WHERE installation_id=? AND message_id=?",
                        (state, installation_id, message_id))
        return int(getattr(cur, "rowcount", 0) or 0) > 0


def inbound_dedup_release(installation_id: str, message_id: str) -> None:
    """放弃去重位（处理失败允许重投）。"""
    with _any_cursor(write=True) as c:
        c.execute("DELETE FROM grape_t_channel_inbound_dedup "
                  "WHERE installation_id=? AND message_id=?", (installation_id, message_id))


def inbound_message_save_and_mark(installation_id: str, message_id: str, chat_id: str,
                                  sender_id: str, text: str) -> None:
    """落库入站消息 + 去重位推进到 marked（同一事务：崩溃重投不丢不重）。"""
    with _any_tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO grape_t_channel_inbound_message "
            "(installation_id, message_id, chat_id, sender_id, text) VALUES (?,?,?,?,?)",
            (installation_id, message_id, chat_id, sender_id, (text or "")[:8000]))
        # marked 用「存在则更新、不存在则插入」的两段式，避开 MySQL 的 ON CONFLICT 语法
        cur = c.execute("UPDATE grape_t_channel_inbound_dedup SET state='marked' "
                        "WHERE installation_id=? AND message_id=?", (installation_id, message_id))
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            c.execute("INSERT OR IGNORE INTO grape_t_channel_inbound_dedup "
                      "(installation_id, message_id, state, claim_token) "
                      "VALUES (?,?, 'marked', '')", (installation_id, message_id))


def chat_binding_get(installation_id: str, channel_chat_id: str) -> tuple | None:
    """取会话绑定 8 列（installation_id, channel_chat_id, chat_type, scope, repo_key,
    agent_type, runtime_session_id, config）。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT installation_id, channel_chat_id, chat_type, scope, repo_key, "
            "agent_type, runtime_session_id, config "
            "FROM grape_t_channel_chat_session_binding "
            "WHERE installation_id=? AND channel_chat_id=?", (installation_id, channel_chat_id))
        return c.fetchone()


def chat_binding_upsert(installation_id: str, channel_chat_id: str, binding_id: str,
                        channel_type: str, chat_type: str, scope: str, repo_key: str,
                        agent_type: str, runtime_session_id: str, config_json: str,
                        now: str) -> str:
    """绑定 upsert（按 installation+chat 定位，主键 id 由调用方给）；返回生效的 id。"""
    with _any_tx() as c:
        c.execute("SELECT id FROM grape_t_channel_chat_session_binding "
                  "WHERE installation_id=? AND channel_chat_id=?",
                  (installation_id, channel_chat_id))
        row = c.fetchone()
        row_id = (row[0] if row else binding_id)
        cur = c.execute(
            "UPDATE grape_t_channel_chat_session_binding SET channel_type=?, chat_type=?, "
            "scope=?, repo_key=?, agent_type=?, runtime_session_id=?, config=?, "
            "last_active_at=?, updated_at=? WHERE id=?",
            (channel_type, chat_type, scope, repo_key, agent_type, runtime_session_id,
             config_json, now, now, row_id))
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            c.execute(
                "INSERT INTO grape_t_channel_chat_session_binding "
                "(id, installation_id, channel_type, channel_chat_id, chat_type, scope, "
                "repo_key, agent_type, runtime_session_id, config, last_active_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, installation_id, channel_type, channel_chat_id, chat_type, scope,
                 repo_key, agent_type, runtime_session_id, config_json, now))
        return row_id


def sender_identity_user(installation_id: str, platform_user_id: str) -> str:
    """发送者身份映射查 grape 用户 id（无映射返回空串）。"""
    with _any_cursor() as c:
        c.execute("SELECT user_id FROM grape_t_channel_sender_identity "
                  "WHERE installation_id=? AND platform_user_id=?",
                  (installation_id, platform_user_id))
        row = c.fetchone()
    return str(row[0]) if row and row[0] else ""


def sender_identity_bind(installation_id: str, platform_user_id: str,
                         stable_id: str, user_id: str) -> None:
    """身份绑定 upsert（(installation_id, platform_user_id) 唯一）；两段式避开 ON CONFLICT。"""
    with _any_tx() as c:
        cur = c.execute("UPDATE grape_t_channel_sender_identity SET user_id=?, "
                        "platform_stable_user_id=? WHERE installation_id=? AND platform_user_id=?",
                        (user_id, stable_id or "", installation_id, platform_user_id))
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            c.execute("INSERT OR IGNORE INTO grape_t_channel_sender_identity "
                      "(installation_id, platform_user_id, platform_stable_user_id, user_id) "
                      "VALUES (?,?,?,?)",
                      (installation_id, platform_user_id, stable_id or "", user_id))


def mapped_user_by_channel(open_id: str, channel_type: str = "") -> str:
    """按渠道外部 id 取 grape 用户 id（列名白名单：钉钉 dingtalk_user_id，其余 feishu_open_id）。"""
    if not open_id:
        return ""
    col = "dingtalk_user_id" if (channel_type or "") == "dingtalk" else "feishu_open_id"
    with _any_cursor() as c:
        c.execute(f"SELECT user_id FROM unified_user_mapping WHERE {col}=? LIMIT 1", (open_id,))
        row = c.fetchone()
    return str(row[0]) if row and row[0] else ""


def installation_active_configs_any() -> list[tuple]:
    """活跃安装的 (id, config)，按拓扑自适应连接（替代需要外部 conn 的旧签名）。"""
    with _any_cursor() as c:
        c.execute("SELECT id, config FROM grape_t_channel_installation WHERE status='active'")
        return [tuple(r) for r in (c.fetchall() or [])]


def installation_set_config_any(cfg_json: str, row_id) -> int:
    """就地写回某安装行的 config；返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("UPDATE grape_t_channel_installation SET config=? WHERE id=?",
                        (cfg_json, row_id))
        return int(getattr(cur, "rowcount", 0) or 0)


def installation_active_installs_any() -> list[tuple]:
    """活跃安装六列 (id, scope, owner_user_id, repo_key, config, status)。"""
    with _any_cursor() as c:
        c.execute("SELECT id, scope, owner_user_id, repo_key, config, status "
                  "FROM grape_t_channel_installation WHERE status='active'")
        return [tuple(r) for r in (c.fetchall() or [])]
