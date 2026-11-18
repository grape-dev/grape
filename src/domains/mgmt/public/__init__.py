"""mgmt 域对外 lib（读侧）：允许列表约束的通用单表查询 + 常用封装。

安全约束：
- 只能访问 `_TABLES` 中登记的表与列（不是开放 SQL 入口，避免把任意 SQL 透传给 lib）
- 一律参数化（%s），不接受拼接的 where 片段中的值
- 只返回元组/字典，不返回游标

用途：runtime / insight 进程在 lib 模式下读 mgmt 库（三库三机）。
"""
from __future__ import annotations

import re
from contextlib import contextmanager

from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number  # Issue #3055

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
    "cat_l1_id", "cat_l2_id",
    "recommended_template", "nodes_json", "onboarding", "tags", "author", "content_hash",
    "obs_icon_key", "preset_custom_fields",
)

#: 场景内容字段的旧键名 → 列名（runtime 场景同步沿用 market_desc / tags_str 两个历史键）
_SCENE_FIELD_ALIASES: dict[str, str] = {
    "market_desc": "market_description",
    "tags_str": "tags",
}
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
                  (int(knowledge_id), scene_id, agent_type, native_issue_number(issue_num), created_by))
        return int(c.rowcount or 0)


def delete_event_knowledge(repo_key: str, scope: str, agent_type: str, issue_num: int) -> dict[str, int]:
    """删除某事件的知识记录：先按 knowledge_id 删 ref，再删主表（顺序与原实现一致）。"""
    with db_registry.cursor("mgmt") as c:
        params = (repo_key, scope, agent_type, native_issue_number(issue_num))
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
    # 历史键名（market_desc / tags_str）先映射成列名，否则这两列会被静默丢掉
    payload = {_SCENE_FIELD_ALIASES.get(k, k): v for k, v in (payload or {}).items()}
    cols = [c for c in _SCENE_CONTENT_COLS if c in payload]
    with db_registry.cursor("mgmt") as cur:
        cur.execute("SELECT id, content_hash, tags, cat_l1_id, cat_l2_id FROM scene_market "
                    "WHERE name=%s AND source=%s LIMIT 1", (name, source))
        row = cur.fetchone()
        if row:
            sid, old_hash, old_tags = row[0], (row[1] or ""), (row[2] or "")
            old_l1, old_l2 = (row[3] if len(row) > 3 else 0), (row[4] if len(row) > 4 else 0)
            if payload.get("content_hash") and payload.get("content_hash") == old_hash:
                return {"action": "skipped", "id": sid, "changed": 0}
            if not cols:
                return {"action": "skipped", "id": sid, "changed": 0}
            if "tags" in payload:
                # Issue #2761：同步写入只补 auto 标签，保留人工(manual)标签
                from src.kernel.tags import encode_tags, merge_tags  # noqa: PLC0415
                payload["tags"] = encode_tags(merge_tags(old_tags, payload["tags"]))
            if "cat_l1_id" in payload:
                # Issue #2761：一级未变且来源未给二级时保留旧二级（未命中不覆盖旧值）
                from src.kernel.category import sync_category_target  # noqa: PLC0415
                payload["cat_l1_id"], payload["cat_l2_id"] = sync_category_target(
                    payload.get("cat_l1_id"), payload.get("cat_l2_id"), old_l1, old_l2)
                if "cat_l2_id" not in cols:
                    cols.append("cat_l2_id")
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


def category_dict_rows(scope: str = "") -> list[dict[str, Any]]:
    """统一分类字典（Issue #2761）：供 runtime 同步层按 slug 解析分类，不直接读 mgmt 表。"""
    scope = (scope or "").strip().lower()
    sql = ("SELECT id, parent_id, level, name, slug, applies_to FROM grape_t_category_dict "
           "WHERE is_active=1")
    params: list[Any] = []
    if scope in ("scene", "skill"):
        sql += " AND applies_to IN ('both', ?)"
        params.append(scope)
    sql += " ORDER BY level, parent_id, id"
    with _any_cursor() as c:
        c.execute(sql, tuple(params))
        return [{"id": int(r[0]), "parent_id": int(r[1] or 0), "level": int(r[2] or 1),
                 "name": r[3], "slug": r[4] or "", "applies_to": r[5] or "both"}
                for r in (c.fetchall() or [])]


def category_dict_active_map(scope: str = "") -> dict[str, dict[str, Any]]:
    """slug → {id, level, parent_id}（同步层用）。"""
    return {r["slug"]: {"id": r["id"], "level": r["level"], "parent_id": r["parent_id"]}
            for r in category_dict_rows(scope) if r.get("slug")}


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
           # 需求中心（runtime/issues_center）外域读：仓库镜像 + 平台登录名（任意拓扑）
           "mapping_user_id_preferring_account", "platform_login_by_user_id_any",
           "repo_key_exists_any", "repo_keys_existing_any",
           # 用户映射（unified_user_mapping）双拓扑读写薄封装（conn=None → 自有 DSN）
           "resolve_feishu_open_id", "list_user_mappings", "list_feishu_name_open_id_pairs",
           "bind_feishu_mapping_row", "sync_user_mapping", "ensure_user_mapping_schema",
           # 飞书通道列（runtime/im/channel_directory 调用；任意拓扑）
           "write_back_feishu_identity", "feishu_open_id_by_user",
           "user_mapping_row_by_open_id", "feishu_open_id_by_platform_column",
           "clear_feishu_by_app", "clear_feishu_by_user", "set_gitcode_nickname_any",
           "upsert_scene_content", "set_scene_icon", "set_scene_deploy_url", "set_scene_nodes", "mark_default_featured", "list_baseline_knowledge_files", "list_knowledge_obs_keys",
           "category_dict_rows", "category_dict_active_map",
           "get_official_scene_id", "count_rows", "count_official_scenes", "list_enabled_autopilot_tasks", "get_autopilot_task_brief",
           # P2 服务接口补齐（N6/N8/N9/N10）
           "repo_bundle", "get_account_public", "get_knowledge_entry", "user_mapping_batch",
           # W2 渠道写原语
           "acquire_channel_lease", "renew_channel_lease", "release_channel_lease",
           # 渠道租约只读（runtime/im/outbox 归属过滤；任意拓扑）
           "lease_owner_by_key", "lease_owned_installations_by_owner",
           "release_all_leases", "claim_inbound_dedup", "upsert_channel_installation",
           "upsert_chat_session_binding", "upsert_sender_identity",
           "add_event_action_filter", "update_event_action_filter",
           "delete_event_action_filter",
           # W3 派生写
           "add_user_feedback", "save_inbound_message", "upsert_outbound_card",
           # 任意拓扑具名查询（`?` 占位符；runtime/kernel 读 mgmt 表不再留兜底 SQL）
           "read_one_any", "read_all_any", "exec_any",
           # 审计流水（insight/observability/events_view 的 L2 audit 来源；任意拓扑）
           "audit_recent_rows",
           "mapping_user_id_by_column", "account_id_by_platform_login",
           "repo_user_id_by_platform_login", "first_join_user_id_by_repo",
           "user_repo_keys_by_username",
           "list_active_scene_nodes", "list_active_scene_bindings",
           "list_enabled_autopilot_engine_ids", "list_autopilot_engine_ids_nonempty",
           "set_autopilot_engine_ids_any",
           # 事件知识清理 / 账户名（runtime/routes/events.py；任意拓扑）
           "knowledge_obs_keys_scoped", "knowledge_ids_scoped",
           "delete_event_knowledge_any", "account_username_by_id_any",
           # autopilot 任务创建者解析（runtime/pipeline/account_env.py；任意拓扑）
           "user_account_username_by_id_any",
           # 渠道安装表解绑/回显（runtime/routes/chat.py；任意拓扑）
           "installation_other_refs_any", "installation_holders_any",
           "installation_user_channel_rows_any", "clear_user_mapping_feishu_any",
           "installation_repo_app_ids_any", "installation_repo_keys_by_app_any",
           # IM 通知扇出目标解析（runtime/im/notify.py；任意拓扑）
           "installation_user_active_rows_any", "installation_p2p_chat_ids_any",
           # 互动卡片台账（runtime/im/engine/outbound_cards.py；任意拓扑）
           "outbound_card_put_any", "outbound_card_chat_row_any",
           "delete_feishu_onboard_task_any",
           # IM 身份解析 / 巡检执行卡（runtime/im/handler_async.py；任意拓扑）
           "sender_identity_user_by_app_any", "installation_owner_by_app_any",
           # 安装枚举来源（runtime/im/engine/installations.py；任意拓扑）
           "installation_provider_rows_any",
           # 场景节点按行号覆写/读取（runtime/engine/scene_nodes_write.py；任意拓扑，游标可选）
           "scene_nodes_write_by_id", "scene_nodes_row_by_id_any",
           "autopilot_task_brief_any", "scene_names_by_repo_any",
           # 能效看板取数（insight/observability/efficiency.py；任意拓扑）
           "account_name_rows_by_ids", "scene_display_name_rows",
           # 巡检任务调度（runtime/engine/scheduler.py；任意拓扑）
           "autopilot_subscribers_any", "list_enabled_autopilot_tasks_any",
           "autopilot_task_row_any", "autopilot_task_last_run_any",
           "set_autopilot_run_info_any",
           # 反馈状态联动（api/feedback_status.py；任意拓扑）
           "feedback_external_sync_page", "feedback_mark_status_if_live",
           "feedback_internal_sync_page", "feedback_apply_statuses",
           # 反馈 → Issue 转换（api/feedback_issue_converter.py；任意拓扑）
           "feedback_pending_conversions", "feedback_mark_converted",
           "feedback_claim_row", "feedback_release_claim", "feedback_list_claims",
           "feedback_mark_converted_from_claim",
           # 场景计数（api/session_store.py 首次托管判定；任意拓扑）
           "count_active_scenes_any",
           # 登录白名单判定（kernel/config.py；任意拓扑）
           "login_whitelist_count", "login_whitelist_row_match",
           "login_whitelist_legacy_column", "login_whitelist_legacy_column_match",
           # baseline 知识 upsert + 全局 ref（runtime/project_info/builder.py；任意拓扑）
           "upsert_baseline_knowledge_any", "ensure_baseline_knowledge_ref_any",
           # baseline 知识文件行 / 账号 id（runtime/engine/service.py；任意拓扑）
           "baseline_knowledge_files_any",
           "account_id_by_username_valid_any", "account_id_by_username_ci_any",
           "account_id_by_username_first_any",
           # OAuth 身份原值（api/deps.py 取 token / 预刷新判定；任意拓扑）
           "oauth_token_status_any", "token_expires_at_any", "stored_access_token_any",
           "mark_external_identity_expired_any",
           # 事件动作过滤器启用规则（api/event_rule.py 的 L1 准入判定；任意拓扑）
           "event_action_filter_enabled_rows",
           # 流水线责任人展示（runtime/routes/pipeline.py；任意拓扑）
           "feishu_name_by_platform_column", "repo_bot_usernames_any",
           # 仓库所属 org（runtime/routes/tasks.py；任意拓扑：自有 DSN / 单库同一句）
           "repo_org_id_any",
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
    # （`autopilot_tasks_e3fa0b` / `autopilot_subscriptions_13648d` 已下线：调度器改走
    #   `_ANY_QUERIES` 的 `autopilot_task_full_row` / `autopilot_subscribers_by_task`）
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
    # （`autopilot_tasks_enabled_full` / `autopilot_task_last_run` 的 `%s` 版本已下线：
    #   调度器改走 `_ANY_QUERIES` 同名可移植语句，两条拓扑同一份 SQL，不留 MySQL 专用副本）
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
def _owner_cursor(cursor: Any = None):
    """能效看板取数游标：单库/测试拓扑复用调用方游标，自有 DSN 时忽略它。

    能效看板模块（insight/observability/efficiency）的设计契约是「喂一个 cursor +
    SQLite 建表即可单测」。单库拓扑下调用方游标与进程主库本就是同一个库，复用它省
    一次嵌套加锁并保住调用方的事务快照；三库拓扑（本域有自有 DSN）下调用方游标指向
    insight 进程库（没有本域表），必须忽略 —— 那正是原来 1146 的来源。
    """
    if cursor is not None and not db_registry.uses_own_dsn("mgmt"):
        yield cursor
        return
    with _any_cursor() as c:
        yield c


@contextmanager
def _owner_write_cursor(cursor: Any = None):
    """写原语的取游标口径（配套 `_owner_cursor` 的读侧）。

    - 单库 / 测试拓扑 + 调用方传了游标 → 复用调用方游标，**提交责任留在调用方**
      （调用点原本就在自己连接上 `commit()`，语义逐字不变）；
    - 自有 DSN 拓扑 → 忽略调用方游标，走 `db_registry.cursor("mgmt")`（共享连接
      autocommit，语句即提交）—— 忽略那一跳正是 1146 的修复；
    - 单库拓扑但调用方没传游标 → 走 `_any_cursor(write=True)` 由本域提交。
      少了这一支，`cursor=None` 的调用方会**静默丢写**（进程库游标不提交）。
    """
    if cursor is not None and not db_registry.uses_own_dsn("mgmt"):
        yield cursor
        return
    with _any_cursor(write=True) as c:
        yield c


@contextmanager
def _same_domain_write_cursor(cursor: Any = None):
    """写游标：**调用方与表同库**时复用其游标，真跨域才换本域连接。

    `_owner_write_cursor` 只在「本域没有自有 DSN」时复用调用方游标；但在 **mgmt 进程内**
    （`current_domain()=="mgmt"` + 自有 DSN）调用方的进程库就是本域库——`mgmt_lib(
    foreign_only=True)` 正是为此返回 None，把同域写留在调用方本地事务里。若照
    `_owner_write_cursor` 一味忽略游标，写会落到 autocommit 的独立连接，调用方随后的
    `rollback()` 就管不到它（事务边界被悄悄改掉）。故这里多一条「同域」判据：
      - 单库 / 视图期 / 测试（无自有 DSN）→ 复用调用方游标；
      - mgmt 进程内（同域）→ 复用调用方游标；
      - 其它域的进程 + 自有 DSN → 忽略调用方游标（那是外域库句柄，写不到本域表），
        走 `_any_cursor(write=True)` 由本域提交。
    提交责任在复用分支里仍留在调用方（与原 SQL 语义逐字一致）。
    """
    if cursor is not None and (not db_registry.uses_own_dsn("mgmt")
                               or db_registry.current_domain() == "mgmt"):
        yield cursor
        return
    with _any_cursor(write=True) as c:
        yield c


@contextmanager
def _same_domain_read_cursor(cursor: Any = None):
    """读游标：**调用方与表同库**时复用它，真跨域才换本域连接（读侧配套 `_same_domain_write_cursor`）。

    `_owner_cursor` 只在「本域没有自有 DSN」时复用调用方游标；mgmt 进程内（同域 + 自有
    DSN）它会另开 mgmt 连接，读的是另一条事务快照 —— 调用方在同一事务里刚改过的行就看不见。
    读侧不涉及提交责任，但快照口径同样是原 SQL 语义的一部分，故这里与写侧共用同一条判据：
      - 单库 / 视图期 / 测试（无自有 DSN）→ 复用调用方游标（同一事务快照）；
      - mgmt 进程内（同域）→ 复用调用方游标；
      - 其它域的进程 + 自有 DSN → 忽略调用方游标（那是外域库句柄，看不到本域表）。
    """
    if cursor is not None and (not db_registry.uses_own_dsn("mgmt")
                               or db_registry.current_domain() == "mgmt"):
        yield cursor
        return
    with _any_cursor() as c:
        yield c


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


# ---------- 任意拓扑具名查询（`?` 占位符：SQL 只在 owner 域留一份） ----------
#
# 与 `_READ_QUERIES` 的区别：那是只在「自有 DSN」拓扑可用的 MySQL 方言（`%s`），
# 调用方必须自带本地兜底 SQL —— 兜底 SQL 正是跨域表引用的来源。这里一律可移植
# 方言（`?`），自有 DSN → mgmt 连接；单库 / 视图期 / 测试 → 进程主库，同一份 SQL
# 两条连接都能执行，调用方不需要（也不允许）再留兜底 SQL。

_ANY_QUERIES: dict[str, str] = {
    # D109 平台对象模型（四表归 mgmt 域）：runtime 读平台卡、写 bot 巡检状态都走本域
    # 原语，不再各自用 ORM 直连自己的库（拆库后那是另一份空/陈旧副本）。
    # 凭据列（bot_token_enc / client_*_enc）一律不出域。
    "repo_platform_cards": (
        "SELECT provider, name, dialect, host, api_base, api_auth, webhook_path, event_header, "
        "webhook_verify, webhook_dialect, pr_head_filter, clone_auth, issue_write_form, "
        "branch_create_form, issue_search, git_tree_family, pr_comment_endpoint, "
        "pr_labels_via_issues, workflows_write, legacy_token_symbol, pat_create_url, "
        "token_storage_field, webhook_token_field, bot_username, bot_token_status, "
        "bot_checked_at, owner_fallback, download_base, user_images_base, brand_color "
        "FROM grape_t_repo_platforms WHERE enabled=1 ORDER BY provider"),
    "repo_platform_cards_all": (
        "SELECT provider, name, dialect, host, api_base, api_auth, webhook_path, event_header, "
        "webhook_verify, webhook_dialect, pr_head_filter, clone_auth, issue_write_form, "
        "branch_create_form, issue_search, git_tree_family, pr_comment_endpoint, "
        "pr_labels_via_issues, workflows_write, legacy_token_symbol, pat_create_url, "
        "token_storage_field, webhook_token_field, bot_username, bot_token_status, "
        "bot_checked_at, owner_fallback, download_base, user_images_base, brand_color "
        "FROM grape_t_repo_platforms ORDER BY provider"),
    "repo_platform_bot_status_set": (
        "UPDATE grape_t_repo_platforms SET bot_token_status=?, bot_checked_at=?, updated_at=? "
        "WHERE provider=?"),
    # 平台身份（external_identities 归本域）：按平台 login 取账号 id（FR-21.2）。
    # 原调用方 runtime/im/feishu_async 是「mgmt_lib 优先 + 进程库兜底 SQL」双分支，
    # 兜底那句只在 lib 不可用时执行（单库/视图期），却正是跨域表引用的来源；且三库
    # 拓扑下 lib 不可用时 runtime 库根本没有 external_identities（视图 0 → 1146），
    # 群成员映射会静默失配。上移后两条拓扑跑同一句，调用方不再留 SQL。
    "external_user_id_by_external_login": (
        "SELECT ei.grape_user_id FROM external_identities ei "
        "WHERE ei.provider=? AND ei.external_login=? AND ei.status=1 LIMIT 1"),
    # 用户映射：按平台列取映射用户（列名由本域白名单挑选，调用方不得拼列名）
    "um_user_id_by_gitcode_user": (
        "SELECT user_id FROM unified_user_mapping "
        "WHERE gitcode_user=? AND gitcode_user!='' AND user_id!='' AND user_id!='0' LIMIT 1"),
    "um_user_id_by_github_login": (
        "SELECT user_id FROM unified_user_mapping "
        "WHERE github_login=? AND github_login!='' AND user_id!='' AND user_id!='0' LIMIT 1"),
    # 用户映射：优先「登录账号存在且启用」的那条映射（issues_center 原语句的 EXISTS 子句）。
    # 单列 um.user_id、谓词与 ORDER BY um.user_id LIMIT 1 逐字照抄；列名仍由本域白名单挑选。
    "um_account_user_id_by_gitcode_user": (
        "SELECT um.user_id FROM unified_user_mapping um "
        "WHERE um.gitcode_user=? "
        "AND (um.user_id IS NOT NULL AND um.user_id != '' AND um.user_id != '0') "
        "AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=um.user_id AND ga.status=1) "
        "ORDER BY um.user_id LIMIT 1"),
    "um_account_user_id_by_github_login": (
        "SELECT um.user_id FROM unified_user_mapping um "
        "WHERE um.github_login=? "
        "AND (um.user_id IS NOT NULL AND um.user_id != '' AND um.user_id != '0') "
        "AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=um.user_id AND ga.status=1) "
        "ORDER BY um.user_id LIMIT 1"),
    # 用户 → 平台登录名（gitcode 优先、空则 github；COALESCE/NULLIF 照抄原语句）
    "um_platform_login_by_user": (
        "SELECT COALESCE(NULLIF(gitcode_user,''), NULLIF(github_login,'')) "
        "FROM unified_user_mapping WHERE user_id=? AND user_id!='' AND user_id!='0' LIMIT 1"),
    # ── 飞书通道列（channel_directory 原「mgmt_lib 优先 + 本地兜底 SQL」的兜底侧上移）──
    # 兜底 SQL 只在单库/视图期执行，却正是跨域表引用的来源；上移到 owner 域后
    # 自有 DSN 与单库拓扑跑同一份语句，调用方不再保留任何 um SQL。
    "um_insert_ignore_user": "INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)",
    "um_set_feishu_by_user": (
        "UPDATE unified_user_mapping SET feishu_open_id=?, feishu_name=?, app_id=?, "
        "updated_at=datetime('now') WHERE user_id=? "
        "AND (user_id IS NOT NULL AND user_id != '' AND user_id != '0')"),
    "um_feishu_open_id_by_user": (
        "SELECT feishu_open_id FROM unified_user_mapping "
        "WHERE user_id=? AND feishu_open_id!='' LIMIT 1"),
    "um_row_by_feishu_open_id": (
        "SELECT user_id, gitcode_user, feishu_name, gitcode_nickname "
        "FROM unified_user_mapping WHERE feishu_open_id=? LIMIT 1"),
    "um_clear_feishu_by_app": (
        "UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', "
        "updated_at=? WHERE app_id=?"),
    "um_clear_feishu_by_user": (
        "UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', "
        "updated_at=? WHERE user_id=?"),
    "um_set_nickname_by_user": (
        "UPDATE unified_user_mapping SET gitcode_nickname=?, updated_at=datetime('now') "
        "WHERE user_id=?"),
    # um 名字域兜底（FR-CQ-1）：平台列 → 该列上的 open_id，列名只在本域白名单挑
    # um 名字域兜底（FR-17.1）：平台列 → 该列上的飞书昵称，列名只在本域白名单挑。
    # 原调用点（runtime pipeline 责任人展示）用 f-string 拼列名；空列名拼出的
    # `WHERE =?` 语法错被吞成「回退平台用户名」，故此处未登记列返回空串，由调用方回退。
    "um_feishu_name_by_gitcode_user": (
        "SELECT feishu_name FROM unified_user_mapping "
        "WHERE gitcode_user=? AND feishu_name!='' LIMIT 1"),
    "um_feishu_name_by_github_login": (
        "SELECT feishu_name FROM unified_user_mapping "
        "WHERE github_login=? AND feishu_name!='' LIMIT 1"),
    "um_feishu_open_id_by_gitcode_user": (
        "SELECT feishu_open_id FROM unified_user_mapping "
        "WHERE gitcode_user=? AND feishu_open_id!='' LIMIT 1"),
    "um_feishu_open_id_by_github_login": (
        "SELECT feishu_open_id FROM unified_user_mapping "
        "WHERE github_login=? AND feishu_open_id!='' LIMIT 1"),
    # 平台 login → 账号 id（账号名≠平台 login 时以 external_identities 为准）
    "account_id_by_platform_login": (
        "SELECT ga.id FROM grape_accounts ga "
        "WHERE ga.id IS NOT NULL AND ga.id NOT IN ('', '0') AND ga.status=1 "
        "AND EXISTS (SELECT 1 FROM external_identities ei "
        "WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
        "AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='') LIMIT 1"),
    # 仓库成员：平台 login → user_id（三级回退的最后一级，未注册用户靠它命中）
    "repo_user_id_by_platform_login": (
        "SELECT ur.user_id FROM user_repos ur "
        "JOIN grape_accounts ga ON ga.id = ur.user_id "
        "WHERE ga.status=1 "
        "AND EXISTS (SELECT 1 FROM external_identities ei "
        "WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
        "AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='') "
        "AND (ur.repo_key=? OR ur.repo_key=?) AND ur.is_join=1 LIMIT 1"),
    "first_join_user_id_by_repo": (
        "SELECT user_id FROM user_repos WHERE (repo_key=? OR repo_key=?) AND is_join=1 "
        "ORDER BY user_id LIMIT 1"),
    "user_repo_keys_by_username": (
        "SELECT ur.repo_key FROM user_repos ur JOIN grape_accounts ua ON ua.id = ur.user_id "
        "WHERE LOWER(ua.username)=LOWER(?) AND ur.is_join = 1"),
    # 场景 / 自动化任务（管理域表；runtime 的引擎绑定维护读）
    "active_scene_nodes": "SELECT id, nodes_json FROM devops_pipeline_scene WHERE is_active=1",
    # 场景节点按行号覆写（runtime 引擎绑定维护的写：recovery / engine_select）。
    # 原实现是调用方本地游标上的兜底句，三库拓扑下进程库没有本表（视图 0 → 1146）；
    # 上移后两条拓扑同一份语句，参数序 `(nodes_json, updated_at 值, id)` 与原句逐字一致
    # ——注意 `updated_at` 是**调用方算好的值**，不是 NOW()。
    "scene_nodes_write_by_id": (
        "UPDATE devops_pipeline_scene SET nodes_json=?, updated_at=? WHERE id=?"),
    "active_scene_bindings": (
        "SELECT id, repo_key, scene_id, nodes_json FROM devops_pipeline_scene WHERE is_active=1"),
    "enabled_autopilot_engine_ids": (
        "SELECT id, name, engine_runtime_ids FROM autopilot_tasks WHERE enabled=1"),
    "autopilot_engine_ids_nonempty": (
        "SELECT id, engine_runtime_ids FROM autopilot_tasks WHERE engine_runtime_ids != ''"),
    "autopilot_engine_ids_set": "UPDATE autopilot_tasks SET engine_runtime_ids=? WHERE id=?",
    # ── 知识关联读（scene_knowledge_ref × agent_shared_knowledge，两张都属 mgmt）──
    # 同域 JOIN 只能留在 owner 侧：调用方原先在 kernel 里 JOIN 两张 mgmt 表，
    # 三库拆分后既读不到也不该读。列序与 kernel 原查询逐字一致（调用方按下标取值）。
    "knowledge_ref_manual_exists": (
        "SELECT COUNT(*) FROM scene_knowledge_ref r "
        "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
        "WHERE r.enabled = 1 AND (r.scene_id = ? OR r.scene_id = '') "
        "AND (r.agent_type = ? OR r.agent_type = '') "
        "AND (r.issue_num = ? OR r.issue_num = 0) "
        "AND k.repo_key = ? AND k.source = 'manual' "
        "AND k.status = 'accepted' AND k.archived = 0"),
    "knowledge_ref_agent_rows": (
        "SELECT DISTINCT k.id, k.scope, k.source, k.obs_key, k.file_path, k.content_hash "
        "FROM scene_knowledge_ref r "
        "JOIN agent_shared_knowledge k ON r.knowledge_id = k.id "
        "WHERE r.enabled = 1 AND (r.scene_id = ? OR r.scene_id = '') "
        "AND (r.agent_type = ? OR r.agent_type = '') "
        "AND (r.issue_num = ? OR r.issue_num = 0) "
        "AND k.repo_key = ? AND k.source = 'agent' "
        "AND k.status = 'accepted' AND k.archived = 0"),
    # 仓库默认场景（issue_state 无 Issue 级快照时的回退源）
    "repo_dev_scene": "SELECT dev_scene FROM managed_repos WHERE repo_key=?",
    # 仓库键存在性（issues_center 的镜像仓查找：单键取回原值 / 多键取交集）
    "repo_key_row": "SELECT repo_key FROM managed_repos WHERE repo_key=? LIMIT 1",
    # 仓库 owner/repo 名（webhook 删仓前取原名做级联清理；列序与 runtime 原查询逐字一致）
    "repo_owner_name": "SELECT repo_owner, repo_name FROM managed_repos WHERE repo_key=?",
    # 仓库登记探活（A2A 真伪校验）：原语句只判「有行」，故与 repo_key_row 分开登记——
    # 后者在空键时短路返回空串，空键行若存在语义不同，这里保留逐字 SQL。
    "repo_registered_flag": "SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1",
    # 幂等补录（三库拓扑下 runtime 进程无 managed_repos；INSERT OR IGNORE 由
    # mysql_compat 翻译为 INSERT IGNORE，两条后端 rowcount 语义一致：1=新建 / 0=已存在）
    "repo_insert_ignore": (
        "INSERT OR IGNORE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
        "VALUES (?,?,?,?)"),
    # 场景市场（runtime 的场景同步读/写；列序与 scene_sync 原查询逐字一致）
    "scene_official_id_by_name": (
        "SELECT id FROM scene_market WHERE name=? AND source='official' LIMIT 1"),
    "scene_official_row_by_name": (
        "SELECT id, version, content_hash, is_active FROM scene_market "
        "WHERE name=? AND source='official' LIMIT 1"),
    "scene_official_id_names": "SELECT id, name FROM scene_market WHERE source='official'",
    "scene_featured_official_count": (
        "SELECT COUNT(*) FROM scene_market WHERE source='official' AND is_featured=1 AND is_active=1"),
    "scene_set_icon_by_id": "UPDATE scene_market SET icon=?, obs_icon_key=? WHERE id=?",
    # ── 流水线场景 / 托管仓库（runtime 事件管道消费：QA 节点、PR 订阅、部署登记）──
    # 列序与 event_pipeline 原查询逐字一致（调用方按下标取值）；WHERE 通配口径照抄，
    # 包括「不加 is_active」的那条（QA skill 取节点时历史口径就是不过滤启用位）。
    "scene_nodes_by_id": (
        "SELECT nodes_json FROM devops_pipeline_scene WHERE id=?"),
    "scene_nodes_by_repo_scene": (
        "SELECT nodes_json FROM devops_pipeline_scene WHERE repo_key=? AND scene_id=?"),
    "pr_subscription_scenes": (
        "SELECT scene_id, nodes_json, advanced_options FROM devops_pipeline_scene "
        "WHERE repo_key=? AND pr_subscription_enabled=1 AND is_active=1"),
    "scene_active_nodes_by_repo_scene": (
        "SELECT scene_id, nodes_json FROM devops_pipeline_scene "
        "WHERE repo_key=? AND scene_id=? AND is_active=1"),
    "scene_linked_repos": (
        "SELECT linked_repos, advanced_options FROM devops_pipeline_scene "
        "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1"),
    "scene_active_flags": (
        "SELECT is_active FROM devops_pipeline_scene "
        "WHERE scene_id=? AND (repo_key=? OR repo_key='')"),
    # 仓库在用场景计数（api 侧「首次托管是否已有默认场景」判定）。原语句在 api 进程里
    # 用进程主库游标直发，三库拆分后 api 库没有本表（视图 0）→ 1146 被外层 except 吞成
    # 「场景数 0」→ 每次托管都重放 apply_market_scene_by_name（幂等但白跑一遍建场景链）。
    # 谓词/过滤位与调用方原语句逐字一致（保留 is_active=1，停用行不算已存在）。
    "scene_active_count_by_repo": (
        "SELECT COUNT(*) FROM devops_pipeline_scene WHERE repo_key=? AND is_active=1"),
    # 仓库下所有在用场景的 linked_repos（A2A 关联仓信任域）：原语句按 repo_key 全量取
    # 多行，无 LIMIT，列序与 runtime 原查询逐字一致（与上面的单场景 scene_linked_repos 分列）
    "scene_linked_repos_by_repo": (
        "SELECT linked_repos, advanced_options FROM devops_pipeline_scene "
        "WHERE repo_key=? AND is_active=1"),
    # 场景在指定仓库下的在用性（A2A 目标场景存在性快判，列序逐字一致）
    "scene_id_active_by_repo_scene": (
        "SELECT scene_id FROM devops_pipeline_scene "
        "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1"),
    "scene_deploy_url_set": (
        "UPDATE devops_pipeline_scene SET deploy_url=? "
        "WHERE scene_id=? AND repo_key=? AND is_active=1"),
    "repo_bot_username": (
        "SELECT bot_username FROM managed_repos WHERE repo_key=? AND bot_username!='' LIMIT 1"),
    # 多仓库批量 bot 用户名（runtime 流水线一次取候选仓库的 bot 集合，消 N+1）。
    # 占位符个数由本域按批次补，SQL 文本仍只此一份；`{ph}` 不是表名/列名，不接受调用方拼接。
    "repo_bot_usernames_in": (
        "SELECT bot_username FROM managed_repos "
        "WHERE repo_key IN ({ph}) AND bot_username!=''"),
    "repo_org_id": (
        "SELECT org_id FROM managed_repos WHERE repo_key=? ORDER BY org_id DESC LIMIT 1"),
    "repo_deploy_url_set": "UPDATE managed_repos SET deploy_url=? WHERE repo_key=?",
    # ── 存量场景绑定修复（Issue #541；api/scene_binding_repair.py 一次性迁移）──
    # 三张表都归本域，原实现在 api 进程里用进程主库游标直发：三库拆分后 api 进程库
    # 没有这些表（视图 0）→ 整段扫描被 except 吞成「0 修复」，Issue #541 的修复
    # 静默失效。列序/谓词/排序与原语句逐字照抄（含 is_active=1 与二级 id 排序）。
    "scene_active_official_id_by_name": (
        "SELECT id FROM scene_market "
        "WHERE name=? AND source='official' AND is_active=1 LIMIT 1"),
    "scene_copy_by_market_id": (
        "SELECT scene_id FROM devops_pipeline_scene "
        "WHERE market_scene_id=? AND repo_key=? AND is_active=1 "
        "ORDER BY updated_at DESC, id DESC LIMIT 1"),
    "repo_bound_scene_rows": (
        "SELECT repo_key, dev_scene FROM managed_repos WHERE dev_scene != ''"),
    "repo_dev_scene_set_cas": (
        "UPDATE managed_repos SET dev_scene=? WHERE repo_key=? AND dev_scene=?"),
    # ── 巡检补偿责任链（api/patrol_compensate.py：载入/抢占/回写/回收/捞单）──
    # 列序与补偿模块原查询逐字一致（调用方按下标解包）；写语句一律普通 UPDATE
    # （禁 SQL CASE 与 `||` 拼接：MySQL 默认 sql_mode 下 || 是逻辑 OR）。
    "patrol_finding_row": (
        "SELECT id, kind, task_id, repo_key, issue_id, status "
        "FROM grape_t_patrol_findings WHERE id=?"),
    "patrol_finding_meta": "SELECT action_meta FROM grape_t_patrol_findings WHERE id=?",
    "patrol_finding_set": (
        "UPDATE grape_t_patrol_findings SET status=?, action_meta=?, updated_at=? WHERE id=?"),
    "patrol_finding_claim": (
        "UPDATE grape_t_patrol_findings SET status='claiming', updated_at=? "
        "WHERE id=? AND status='open'"),
    "patrol_findings_stale_claiming": (
        "SELECT id, action_meta FROM grape_t_patrol_findings "
        "WHERE kind='broken_chain' AND status='claiming' "
        "AND (updated_at='' OR updated_at < ?)"),
    "patrol_finding_reopen": (
        "UPDATE grape_t_patrol_findings SET status='open', action_meta=? WHERE id=?"),
    "patrol_findings_open_ids": (
        "SELECT id FROM grape_t_patrol_findings WHERE kind='broken_chain' AND status='open' "
        "AND (action_meta='' OR updated_at < ?) ORDER BY created_at LIMIT ?"),
    # ── GSB 服务注册表（gsb_service_registry 归 mgmt；各域进程共同注册/发现）──
    # 原实现在 kernel 里直连进程主库：单库拓扑正常，三库拓扑下每个域把注册写进自己那个库
    # ——注册不报错、但跨域发现不到（静默失效，不是 1146）。列序与 kernel 原语句逐字一致。
    "gsb_service_upsert": (
        "INSERT OR REPLACE INTO gsb_service_registry "
        "(service_name, base_url, health_url, version, status, last_heartbeat, metadata) "
        "VALUES (?, ?, ?, ?, 'online', ?, ?)"),
    "gsb_service_heartbeat": (
        "UPDATE gsb_service_registry SET last_heartbeat=?, status='online' "
        "WHERE service_name=?"),
    "gsb_service_online_row": (
        "SELECT * FROM gsb_service_registry WHERE service_name=? AND status='online'"),
    "gsb_services_online": (
        "SELECT * FROM gsb_service_registry "
        "WHERE status='online' AND last_heartbeat > ? ORDER BY service_name"),
    # 实例枚举两个变体（是否含离线行）：谓词与原动态拼接出的两条语句逐字一致
    "gsb_instances": (
        "SELECT * FROM gsb_service_registry "
        "WHERE (service_name = ? OR service_name LIKE ?) ORDER BY service_name"),
    "gsb_instances_online": (
        "SELECT * FROM gsb_service_registry "
        "WHERE (service_name = ? OR service_name LIKE ?) "
        "AND status='online' AND last_heartbeat > ? ORDER BY service_name"),
    "gsb_services_all": "SELECT * FROM gsb_service_registry ORDER BY service_name",
    "gsb_services_online_only": (
        "SELECT * FROM gsb_service_registry WHERE status='online' ORDER BY service_name"),
    "gsb_service_mark_offline": (
        "UPDATE gsb_service_registry SET status='offline' WHERE service_name=?"),
    "gsb_service_purge_stale": (
        "UPDATE gsb_service_registry SET status='offline' "
        "WHERE status='online' AND last_heartbeat < ?"),
    # ── 渠道安装表（grape_t_channel_installation 归 mgmt；runtime 侧 IM 路由的读写原语）──
    # 列序与 runtime/routes/im.py 原语句逐字一致（调用方按下标解包）；写语句一律
    # 可移植 `datetime('now')`（MySQL 方言桥翻译），不写 MySQL 专有 NOW()。
    "installation_status_counts": (
        "SELECT status, COUNT(*) FROM grape_t_channel_installation GROUP BY status"),
    "installation_list_all": (
        "SELECT id, channel_type, scope, owner_user_id, repo_key, config, status, updated_at "
        "FROM grape_t_channel_installation ORDER BY updated_at DESC"),
    # 安装枚举来源（runtime/im/engine/installations.py 的 Supervisor installation_provider）。
    # 八列、列序与原语句逐字一致（调用方按下标解包；config 原文由调用方 json.loads，本层不解析）。
    # 原语句无 ORDER BY，这里也不排序：「枚举顺序」由调用方哈希环归属决定，加排序会改变
    # 扫描口径（首轮接管顺序），属行为变更而非归位。
    "installation_provider_active_rows": (
        "SELECT id, channel_type, scope, owner_user_id, repo_key, config, "
        "config_fingerprint, status FROM grape_t_channel_installation "
        "WHERE status='active'"),
    "installation_owner_by_id": (
        "SELECT scope, owner_user_id, repo_key FROM grape_t_channel_installation WHERE id=?"),
    "installation_detail_by_id": (
        "SELECT channel_type, scope, owner_user_id, repo_key, config "
        "FROM grape_t_channel_installation WHERE id=?"),
    "installation_update_status": (
        "UPDATE grape_t_channel_installation SET status=?, updated_at=datetime('now') "
        "WHERE id=?"),
    "installation_update_config_fp": (
        "UPDATE grape_t_channel_installation SET config=?, config_fingerprint=?, "
        "updated_at=datetime('now') WHERE id=?"),
    "installation_active_configs_by_type": (
        "SELECT config FROM grape_t_channel_installation "
        "WHERE channel_type=? AND status='active'"),
    # chat.py 解绑清理的三条（原在 runtime/routes/chat.py 直发 SQL，或走
    # engine_wiring._connect 的 runtime 库句柄）。列序/谓词与原语句逐字一致：
    # 引用判定「不含本人这条」用 NOT (scope='user' AND owner_user_id=?)，与
    # 原 `AND NOT (scope='user' AND owner_user_id=?)` 同义（可移植方言，两条后端都可执行）。
    "installation_other_refs_count": (
        "SELECT COUNT(*) FROM grape_t_channel_installation "
        "WHERE legacy_app_id=? AND status='active' "
        "AND NOT (scope='user' AND owner_user_id=?)"),
    "installation_holders_by_app": (
        "SELECT scope, owner_user_id FROM grape_t_channel_installation "
        "WHERE legacy_app_id=? AND status='active'"),
    "installation_user_channels": (
        "SELECT channel_type, status, config FROM grape_t_channel_installation "
        "WHERE scope='user' AND owner_user_id=? AND status IN ('active','disabled') "
        "ORDER BY updated_at DESC"),
    # VOD Bot 导航（runtime/im/vod_handler_async.py）：仓库轨安装的两个反查口。
    # 原实现在 runtime 进程里用进程主库直发 SQL —— 三库拓扑下 runtime 库没有本表（视图 0）
    # → 1146 被调用方 `except: return set()/[]` 吞成「该仓库未配置本 bot 的 VOD」
    # 与「/repo 无候选」。谓词/列序/COALESCE 空值过滤逐字照抄（不加 ORDER BY：
    # 原语句是 DISTINCT 无序，分页顺序与原来同口径，不擅自改）。
    "installation_repo_app_ids": (
        "SELECT legacy_app_id FROM grape_t_channel_installation "
        "WHERE scope='repo' AND repo_key=? AND status='active' "
        "AND COALESCE(legacy_app_id,'')<>''"),
    "installation_repo_keys_by_app": (
        "SELECT DISTINCT repo_key FROM grape_t_channel_installation "
        "WHERE scope='repo' AND status='active' AND legacy_app_id=? "
        "AND COALESCE(repo_key,'')<>''"),
    # ── IM 通知扇出目标解析（runtime/im/notify.py；任意拓扑）──
    # 原实现两处只读都走 engine_wiring._connect()（钉 runtime 库的 IM 句柄）读本域两张表：
    # 三库拓扑下该库没有这两张表（视图 0）→ 1146，被调用点 `except → log.warning → 返回
    # 空列表/空串` 吞成「该用户没有可通知渠道」，通知扇出整体静默失效。列序/谓词/排序逐字照抄，
    # 不新增 is_active/status 之外的过滤（原语句也没有），id 原值不在这里转 str（调用方同前）。
    "installation_user_active_rows": (
        "SELECT id, channel_type, config FROM grape_t_channel_installation "
        "WHERE scope='user' AND owner_user_id=? AND status='active' "
        "ORDER BY updated_at DESC"),
    "chat_binding_p2p_channels": (
        "SELECT channel_chat_id FROM grape_t_channel_chat_session_binding "
        "WHERE installation_id=? AND chat_type='p2p' AND channel_chat_id<>'' "
        "ORDER BY updated_at DESC"),
    # 扫码接入任务删除（含 app_secret/qr_url 敏感数据；飞书专属链路）
    "onboard_task_delete_by_app": "DELETE FROM feishu_onboard_task WHERE app_id=?",
    # ── IM 身份解析 / 巡检执行卡（runtime/im/handler_async.py）──
    # 四张表（渠道身份、渠道安装、巡检任务、流水线场景）都归本域：原实现在 runtime 进程里
    # 用进程主库游标直发 SQL，三库拓扑下本域表不在该库（视图 0）→ 1146 被内部 except 吞成
    # 「查不到用户 / 无场景」。身份反查的两表 JOIN 不跨域，仍留一条；巡检执行行与任务表
    # 的跨域 JOIN 已由调用方拆成「runtime 取执行行 + 本域取简报」。谓词/列序逐字照抄
    # （含 D105 的字符串哨兵 '0'：TEXT 列 `>0` 在 MySQL 恒假）。
    "sender_identity_user_by_app": (
        "SELECT i.user_id FROM grape_t_channel_sender_identity i "
        "JOIN grape_t_channel_installation ci ON ci.id=i.installation_id "
        "WHERE ci.legacy_app_id=? AND i.platform_user_id=? "
        "AND i.user_id IS NOT NULL AND i.user_id != '' AND i.user_id != '0' LIMIT 1"),
    "installation_owner_by_app_user_scope": (
        "SELECT owner_user_id FROM grape_t_channel_installation "
        "WHERE legacy_app_id=? AND scope='user' "
        "AND owner_user_id IS NOT NULL AND owner_user_id<>'' LIMIT 1"),
    "autopilot_task_brief_row": (
        "SELECT repo_key, name FROM autopilot_tasks WHERE id=? LIMIT 1"),
    "scene_names_by_repo": (
        "SELECT scene_id, name FROM devops_pipeline_scene "
        "WHERE repo_key=? AND is_active=1 ORDER BY name"),
    # ── 巡检任务调度（runtime/engine/scheduler.py）──
    # 任务表 + 订阅表同属本域，订阅那条 JOIN 不跨域、整条保留。列序/谓词与原语句逐字一致；
    # 写语句只用 `?` 占位符，时间值由调用方在 Python 侧算好传入（不写 NOW()）。
    "autopilot_subscribers_by_task": (
        "SELECT s.user_id, t.name, t.repo_key FROM autopilot_subscriptions s "
        "JOIN autopilot_tasks t ON s.task_id=t.id WHERE s.task_id=?"),
    "autopilot_tasks_enabled_full": (
        "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
        "cron, max_retry, created_by, engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids "
        "FROM autopilot_tasks WHERE enabled=1"),
    "autopilot_task_full_row": (
        "SELECT id, name, repo_key, agent_type, instruction, skill_name, "
        "cron, max_retry, created_by, engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids "
        "FROM autopilot_tasks WHERE id=?"),
    "autopilot_task_last_run": "SELECT last_run_at FROM autopilot_tasks WHERE id=?",
    "autopilot_run_info_set": (
        "UPDATE autopilot_tasks SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?"),
    # ── 事件知识清理 + 账户名解析（runtime/routes/events.py：原「mgmt_lib 优先 + 本地兜底 SQL」
    # 的兜底侧与引擎 scope 子查询上移；列序/谓词与原语句逐字一致，写语句不含 SQL 专有语法）──
    "knowledge_obs_keys_scoped": (
        "SELECT obs_key FROM agent_shared_knowledge "
        "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=? "
        "AND obs_key!='' AND obs_key IS NOT NULL"),
    "knowledge_ids_scoped_portable": (
        "SELECT id FROM agent_shared_knowledge "
        "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?"),
    "knowledge_delete_scoped": (
        "DELETE FROM agent_shared_knowledge "
        "WHERE repo_key=? AND scope=? AND agent_type=? AND issue_num=?"),
    # ── baseline 知识 upsert + 全局 ref（runtime/project_info/builder 的本地兜底路径）──
    # 原实现是「进程主库游标读 id → UPDATE / INSERT → 取 lastrowid → 再补一条
    # scene_knowledge_ref」，三库拓扑下进程库没有这两张表（视图 0 → 1146），
    # 异常被外层 except 吞成「只写了 OBS + 本地文件」，知识行与场景关联静默缺失。
    # 谓词与列序与 builder 原语句逐字一致（id 查询仍靠 fetchone() 取首行，不额外加 LIMIT，
    # 与原实现同口径）；写语句无 SQL 专有语法（无 NOW() / ON DUPLICATE / 库名前缀）。
    "knowledge_baseline_id_any": (
        "SELECT id FROM agent_shared_knowledge "
        "WHERE repo_key=? AND scope='baseline' AND name=? AND archived=0"),
    "knowledge_baseline_refresh": (
        "UPDATE agent_shared_knowledge SET status='accepted', obs_key=?, content_hash=?, "
        "created_by='system', source='system', name=?, description=? WHERE id=?"),
    "knowledge_baseline_insert_any": (
        "INSERT INTO agent_shared_knowledge "
        "(scope, repo_key, agent_type, issue_num, file_path, status, obs_key, content_hash, "
        "created_by, source, name, description) "
        "VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'system', 'system', ?, ?)"),
    # baseline 全局关联：scene_id='' / agent_type='' / issue_num=0 / created_by='system'
    # 全是原语句的常量；OR IGNORE 由 mysql_compat 翻译为 INSERT IGNORE（rowcount 1=新建 / 0=已存在）
    "knowledge_baseline_ref_ensure": (
        "INSERT OR IGNORE INTO scene_knowledge_ref "
        "(knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
        "VALUES (?, '', '', 0, 1, 'system')"),
    "account_username_by_id": "SELECT username FROM grape_accounts WHERE id=? LIMIT 1",
    # ── 个人账号（`user_accounts`，与登录账号 `grape_accounts` 是**两张表**）──
    # 原消费点是 runtime/pipeline/account_env 的 autopilot「任务创建者」解析：lib 侧走
    # `user_accounts_e2eb04`，lib 不可用时留了一段本地兜底 SQL —— 兜底句正是跨域引用
    # 的来源，且三库拓扑下 runtime 进程库没有 user_accounts（视图 0 → 1146）。
    # 语句逐字照抄原兜底句（无 LIMIT、无 is_disabled/is_active 过滤：任务创建者即便是
    # 已停用账号也要回原名，由调用方的形态校验决定是否采用）。
    # 不可复用 `account_username_by_id`：那张表是登录账号，同 id 空间不同表。
    "user_account_username_by_id": "SELECT username FROM user_accounts WHERE id=?",
    # ── 引擎 scope / claim 过滤取账号 id（runtime/engine/service.py 两处「lib 优先 + 本地兜底」）──
    # 原实现是双分支：lib 侧走 `grape_accounts_0b3fdc`（严格大小写）/ `grape_accounts_d0ac07`
    # （LOWER 双侧），兜底侧是调用方进程主库上的同一段 SQL —— 三库拓扑下进程库没有
    # grape_accounts（视图 0 → 1146，被外层 except 吞成「引擎不可达」/「uid=0」），
    # 且同一判定在两种拓扑下答案不一致。上移后两条拓扑跑同一份语句。
    # `valid`：保留原 0b3fdc 兜底的「id 必须有效」过滤（历史 int 主键的 ''/'0' 哨兵），
    # 大小写取向与账号域自身口径一致（`username_lower` / `get_account_id_by_username`，
    # D32 #348 已把同一判定定为大小写不敏感）。
    "account_id_by_username_valid": (
        "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) "
        "AND id IS NOT NULL AND id NOT IN ('', '0') LIMIT 1"),
    # `ci`：逐字照抄 d0ac07（无 LIMIT、无 id 过滤，首行由 fetchone() 语义决定，
    # 收紧会改变 claim 的 user scope 过滤结果）。
    "account_id_by_username_ci": "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?)",
    # `ci_limit1`：runtime 注册回填 owner_id 的兜底句（`SELECT id FROM grape_accounts
    # WHERE LOWER(username)=LOWER(?) LIMIT 1`）逐字上移——那句原先是 runtime 进程主库
    # 上的跨域 SQL。与上面的 `ci` **不合并**：`LIMIT 1` 是原语句的一部分（同表同名
    # 多行时首行即答案），收紧/放宽都会改「注册回填拿到哪一行」的口径。
    "account_id_by_username_ci_limit1": (
        "SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) LIMIT 1"),
    # baseline 知识文件行（project-info 的 OBS 分支）：原 `list_baseline_knowledge_files` 是
    # 只对自有 DSN 可用的 `%s` 查法，单库拓扑走调用方本地兜底 SQL（跨域引用来源）；
    # 列序 / 谓词 / ORDER BY id DESC 与原语句逐字一致。
    "knowledge_baseline_files": (
        "SELECT file_path, obs_key FROM agent_shared_knowledge "
        "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL "
        "AND archived=0 ORDER BY id DESC"),
    # ── 仓库级 bot token 与登录名（kernel/token_resolver 第 2 级策略）──
    # 原实现在 kernel 里直连进程主库：三库拓扑下进程库没有 managed_repos（无视图 → 1146，
    # 均被内部 except 吞成「无凭据」/「未过期」），或存在同表副本时静默按副本判定。
    # legacy 列名由本域白名单挑选（bot_<provider>_token 只对 gitcode/github 存在物理列），
    # 谓词与列序与原 SQL 逐字一致。
    "repo_bot_token_legacy_gitcode": (
        "SELECT bot_gitcode_token FROM managed_repos "
        "WHERE repo_key=? AND bot_gitcode_token IS NOT NULL AND bot_gitcode_token!='' LIMIT 1"),
    "repo_bot_token_legacy_github": (
        "SELECT bot_github_token FROM managed_repos "
        "WHERE repo_key=? AND bot_github_token IS NOT NULL AND bot_github_token!='' LIMIT 1"),
    "repo_bot_token_status": "SELECT bot_token_status FROM managed_repos WHERE repo_key=? LIMIT 1",
    "repo_bot_token_status_set": "UPDATE managed_repos SET bot_token_status=? WHERE repo_key=?",
    # 用户 token 原始值（token_resolver 的 read_stored_token：读侧解密在调用方，
    # 本域只交出密文；status IN (1, 4) 覆盖「正常 + 标记过期」两种存量口径）
    "ei_stored_access_token": (
        "SELECT access_token FROM external_identities "
        "WHERE grape_user_id=? AND provider=? AND status IN (1, 4) LIMIT 1"),
    # 同表两列原值（api/deps.py 的 OAuth 取 token / 预刷新判定；status 由调用方判定语义）。
    # 原实现在 api 进程主库直发 SQL：三库拓扑下 api 库没有 external_identities（视图 0 → 1146），
    # 被外层 except 吞成「无 token」/「不预刷新」。列序、谓词、无 LIMIT（fetchone 取首行）逐字照抄。
    "ei_oauth_token_status": (
        "SELECT access_token, status FROM external_identities "
        "WHERE grape_user_id=? AND provider=? AND status IN (1, 4)"),
    "ei_token_expires_at": (
        "SELECT token_expires_at FROM external_identities "
        "WHERE grape_user_id=? AND provider=? AND status IN (1, 4)"),
    # 凭据失效标记（runtime/vcs/oauth_401_retry 的 invalid_grant 流程写入）。
    # 原实现是「mgmt lib + 幂等意图」优先、单库拓扑落回 runtime 侧本地 UPDATE ——
    # 兜底句正是跨域表引用来源，且自有 DSN 拓扑下进程库没有该表（无视图 → 1146，
    # 被 except 吞成「标记失败」）。语句上移后两条拓扑跑同一份 SQL；谓词逐字照抄
    # （只改 status=1 的行，status=4 不重复计数，rowcount 语义不变）。
    "ei_expire_mark": (
        "UPDATE external_identities SET status=4 "
        "WHERE grape_user_id=? AND provider=? AND status=1"),
    # ── 代发内容台账（kernel/outbound_ledger；outbound_content_ledger 归 mgmt）──
    # 原实现在 kernel 里用进程主库游标直发 SQL：三库拓扑下进程库没有该表（视图 0 → 1146），
    # 写侧记账被内部 except 吞成 False（只剩进程内 TTL 集合，跨进程/重启即漏拦），
    # 读侧异常同样被吞成「放行」——台账这道防线整体失效且只在日志里可见。
    # 列序与谓词逐字照抄原语句；时间比较的截止值由调用方在 Python 侧算好传参
    # （不写 NOW() / datetime('now','-N days') 之类专有/修饰符方言）。
    "outbound_ledger_insert": (
        "INSERT OR IGNORE INTO outbound_content_ledger "
        "(provider, repo_key, content_type, content_id, issue_number, actor_identity, "
        " origin_channel, origin_task, content_hash, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)"),
    "outbound_ledger_purge_before": (
        "DELETE FROM outbound_content_ledger WHERE created_at < ?"),
    "outbound_ledger_exists_edit": (
        "SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
        "AND content_type=? AND content_id=? AND content_hash=? LIMIT 1"),
    "outbound_ledger_exists_within": (
        "SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
        "AND content_type=? AND content_id=? AND created_at>=? LIMIT 1"),
    "outbound_ledger_exists_id": (
        "SELECT 1 FROM outbound_content_ledger WHERE provider=? AND repo_key=? "
        "AND content_type=? AND content_id=? LIMIT 1"),
    "outbound_ledger_row_by_hash": (
        "SELECT content_id FROM outbound_content_ledger WHERE provider=? "
        "AND content_hash=? AND repo_key=? LIMIT 1"),
    # ── 反馈状态联动（api/feedback_status.py；user_feedback 归 mgmt）───────────
    # 原实现在 api 进程主库直发 SQL：三库拓扑下 user_feedback 在 mgmt 库（视图 0）→
    # 1146 被两处顶层 except 吞成「0 条更新」，反馈状态联动（#673）整体静默失效。
    # 谓词/列序/ORDER BY 逐字照抄；`?` 占位符，两条拓扑同一份 SQL。
    # 内连接那条原语句是 `user_feedback ⋈ issue` 跨域 JOIN，拆分后本域只出左表页，
    # 右表（issue，runtime 域）由调用方用 `issue_stage_status_rows` 取回后做 Python 内连接；
    # `id > ?` 是拆分引入的键集分页锚点（ORDER BY id 不变，最终行集合与 LIMIT 口径一致）。
    "feedback_external_sync_rows": (
        "SELECT f.id, f.status, f.repo_key, f.issue_number, f.issue_url "
        "FROM user_feedback f "
        "WHERE f.source='user' AND f.issue_url!='' "
        "AND (f.issue_id IS NULL OR f.issue_id=0) AND f.is_deleted=0 "
        "AND f.status NOT IN ('closed') "
        "AND f.id > ? ORDER BY f.id LIMIT ?"),
    # 外部联动原实现对每行独立提交（`with db_lock: UPDATE; commit`），保持自提交，
    # `is_deleted=0` 守卫同样逐字保留。
    "feedback_external_mark_status": (
        "UPDATE user_feedback SET status=? WHERE id=? AND is_deleted=0"),
    "feedback_internal_sync_page": (
        "SELECT f.id, f.status, f.issue_id FROM user_feedback f "
        "WHERE f.issue_url!='' AND f.issue_id IS NOT NULL AND f.is_deleted=0 "
        "AND f.id > ? ORDER BY f.id LIMIT ?"),
    # 内部联动原语句没有任何 is_deleted 守卫（已转换行的回写不看软删标记），照抄。
    "feedback_internal_set_status": "UPDATE user_feedback SET status=? WHERE id=?",
    # ── 反馈 → Issue 转换（api/feedback_issue_converter.py；user_feedback 归 mgmt）──
    # 原实现在 api 进程主库直发（`_fetch_pending` 读 + 组内逐行回写）：三库拓扑下
    # api 进程库没有 user_feedback（视图 0 → 1146），待转换队列恒空、转换器整体停摆。
    # 列序（15 列）/ 谓词 / ORDER BY / LIMIT 逐字照抄；只用 `?` 占位符。
    # 入选口径照抄：未转换（issue_url='')、未软删，且 source='agent' 或
    # source='user' 且带仓库上下文（repo_key 非空，Issue #673）—— 不额外加过滤。
    "feedback_pending_rows": (
        "SELECT id, repo_key, task_id, agent_type, skill_name, skill_scope, "
        "title, description, fail_summary, event_seq, issue_id, issue_number, feedback_kind, "
        "source, username "
        "FROM user_feedback WHERE issue_url='' AND is_deleted=0 "
        "AND (source='agent' OR (source='user' AND COALESCE(repo_key,'')!='')) "
        "ORDER BY id LIMIT ?"),
    # 回写守卫 `issue_url=''` 逐字保留：已转换行不被二次覆盖（重放 / 并发安全）。
    "feedback_mark_converted": (
        "UPDATE user_feedback SET issue_url=?, issue_number=?, converted_at=? "
        "WHERE id=? AND issue_url=''"),
    # 占位（claim）：把空 issue_url 改成本次 worker 的占位值，单条条件 UPDATE 即领走该行。
    # 多进程 / 多机（共享同一库）下只有一个能 rowcount=1，这就是跨进程去重的那道闸。
    "feedback_claim_row": (
        "UPDATE user_feedback SET issue_url=? "
        "WHERE id=? AND COALESCE(issue_url,'')=''"),
    # 占位回收（失败放回 / 过期清理）：只清掉当前的占位值，已转换行不受影响。
    "feedback_release_claim": (
        "UPDATE user_feedback SET issue_url='' WHERE id=? AND issue_url LIKE ?"),
    # 列出当前所有占位行（前缀 LIKE），供过期回收逐个判定。
    "feedback_list_claims": (
        "SELECT id, issue_url FROM user_feedback WHERE issue_url LIKE ?"),
    # 回写时同时接受「空」与「本次占位」：占位挡着自己回写会让建出来的单永远记不上。
    "feedback_mark_converted_from_claim": (
        "UPDATE user_feedback SET issue_url=?, issue_number=?, converted_at=? "
        "WHERE id=? AND (issue_url='' OR issue_url LIKE ?)"),
    # ── 登录白名单（kernel/config.py 的登录判定；login_whitelist 归 mgmt）────────
    # 原实现在 kernel 里用进程主库游标直发：三库拓扑下进程库没有 login_whitelist
    # （无视图 → 1146），被 `_is_login_whitelist_enabled` / `is_user_allowed` 的
    # except 吞成「默认关闭」/「降级文件」，白名单开关在独立部署下整体失效。
    # 列序（COUNT(*) / 常量 1）、谓词、LOWER() 大小写口径逐字照抄。
    "login_whitelist_count": "SELECT COUNT(*) FROM login_whitelist",
    "login_whitelist_row_match": (
        "SELECT 1 FROM login_whitelist WHERE provider=? AND LOWER(username)=LOWER(?)"),
    # ── 事件动作过滤器（api/event_rule 的 L1 准入判定）：列集三条变体 ──
    # 列序与原 api 侧三条 SELECT 逐字一致（恒 8 列）；`block_changes` / `vcs_source` 是
    # 迁移期补列（`_ensure_column`），缺列时用字面量 '' 占位 —— 与原来「列不存在就查常量
    # 空串」同义，只是把三份 SQL 收在本域。探测列名只用 `LIMIT 0` 的元数据，不读数据。
    "event_action_filter_columns": "SELECT * FROM event_action_filter LIMIT 0",
    "event_action_filter_enabled": (
        "SELECT event_type, action, allow, actor, require_changes, block_changes, "
        "handle_side_effect, vcs_source FROM event_action_filter "
        "WHERE enabled=1 ORDER BY priority, id"),
    "event_action_filter_enabled_no_vcs": (
        "SELECT event_type, action, allow, actor, require_changes, block_changes, "
        "handle_side_effect, '' FROM event_action_filter "
        "WHERE enabled=1 ORDER BY priority, id"),
    "event_action_filter_enabled_legacy": (
        "SELECT event_type, action, allow, actor, require_changes, '', "
        "handle_side_effect, '' FROM event_action_filter "
        "WHERE enabled=1 ORDER BY priority, id"),
    # ── 渠道租约只读（grape_t_channel_lease 归 mgmt；runtime/im/outbox 的归属过滤）──
    # 与上方 W2 写原语（acquire/renew/release_channel_lease）分开：消费侧只判归属，
    # 不参与抢租约。列序、COALESCE、谓词、ORDER BY updated_at DESC 与字面 LIMIT 200
    # 全部逐字照抄调用方原语句（LIMIT 不做参数化：它是本域对该读口的容量约束）。
    "lease_owner_by_key": (
        "SELECT COALESCE(lease_owner,'') FROM grape_t_channel_lease WHERE lease_key=?"),
    "lease_owned_installations_by_owner": (
        "SELECT installation_id, channel_type FROM grape_t_channel_lease "
        "WHERE lease_owner=? AND installation_id<>'' "
        "ORDER BY updated_at DESC LIMIT 200"),
    # ── 互动卡片台账（grape_t_channel_outbound_card 归 mgmt；runtime/im/engine/
    # outbound_cards 的钉钉 outTrackId → 会话映射）──────────────────────────────
    # 与上方 W3 `upsert_outbound_card`（outbox 投递状态机）**分开登记**：那条是
    # 「不改 payload 的部分更新」（ON DUPLICATE KEY UPDATE 只碰 status/last_error），
    # 这条是卡片台账的「整行替换」（INSERT OR REPLACE），列集也不同 —— 合流会
    # 把 outbox 的 attempt_count/expires_at 一起清掉。列序与原语句逐字一致。
    "outbound_card_put": (
        "INSERT OR REPLACE INTO grape_t_channel_outbound_card "
        "(out_track_id, installation_id, channel_type, channel_chat_id, chat_type, "
        " sender_staff_id, card_template_id) VALUES (?,?,?,?,?,?,?)"),
    "outbound_card_chat_row": (
        "SELECT channel_chat_id, chat_type, sender_staff_id, installation_id, "
        "card_template_id FROM grape_t_channel_outbound_card WHERE out_track_id=?"),
    # ── 审计流水（audit_logs 归 mgmt；insight/observability/events_view 的统一事件视图
    # `audit` 来源，**仅 L2**）──────────────────────────────────────────────────
    # 原实现在 insight 进程主库直发 SQL：三库拓扑下 insight 库没有 audit_logs（视图 0 →
    # 1146），被 `_audit_events` 的 except 吞成「审计来源恒空」且只留一行 warning。
    # 列序 / 谓词 / `ORDER BY seq DESC` / 参数化 LIMIT 与原语句逐字一致；关键字过滤
    # 保留原 `if q:` 的两列 LIKE（event_type / detail），模式值由调用方包通配符后传参。
    "audit_recent_rows": (
        "SELECT id, event_type, user_id, operator_id, ip, created_at, detail "
        "FROM audit_logs ORDER BY seq DESC LIMIT ?"),
    "audit_recent_rows_q": (
        "SELECT id, event_type, user_id, operator_id, ip, created_at, detail "
        "FROM audit_logs WHERE (event_type LIKE ? OR detail LIKE ?) "
        "ORDER BY seq DESC LIMIT ?"),
    # ── 技能分类（skill_category 归 mgmt；runtime/storage/skill_sync 的官方技能同步
    # 需要「启用中的分类」slug→id 映射）────────────────────────────────────────
    # 与 `skill_category_map()` **分开**登记：那条给迁移期回填用（不判 is_active，
    # 只跳过空 slug），这条逐字照抄 skill_sync 原语句的 `WHERE is_active=1` ——
    # 停用的分类不应再把新同步的技能挂上去。列序 `(slug, id)` 与原语句一致。
    "skill_category_active_ids": (
        "SELECT slug, id FROM skill_category WHERE is_active=1"),
}

#: unified_user_mapping 的平台列白名单（列名 → 具名查询）。未知列直接抛错，
#: 与原实现「拼出空列名 → SQL 语法错 → 整函数返回 0」的口径一致，不引入新行为。
_UM_COLUMN_QUERIES: dict[str, str] = {
    "gitcode_user": "um_user_id_by_gitcode_user",
    "github_login": "um_user_id_by_github_login",
}

#: 同一张表的「平台列 → open_id」白名单（channel_directory 的 um 名字域兜底）。
#: 列名由本域挑选，调用方只传 repo_key 推出的列名，不得拼 SQL。
_UM_OPENID_COLUMN_QUERIES: dict[str, str] = {
    "gitcode_user": "um_feishu_open_id_by_gitcode_user",
    "github_login": "um_feishu_open_id_by_github_login",
}

#: 同一张表的「平台列 → feishu_name」白名单（runtime pipeline 责任人展示）。
#: 与 open_id 那张分列：同表同列风格但取值不同，合并会丢「昵称空」与「无 open_id」的区别。
_UM_FEISHU_NAME_COLUMN_QUERIES: dict[str, str] = {
    "gitcode_user": "um_feishu_name_by_gitcode_user",
    "github_login": "um_feishu_name_by_github_login",
}


#: 同一张表的「平台列 → 带登录账号存在性校验的 user_id」白名单（issues_center 优先链：
#: 原 SQL 的 `EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=um.user_id AND ga.status=1)`
#: 属 mgmt 本域子查询，随 SQL 一起留在本域；调用方只传 repo_key 推出的列名）。
_UM_ACCOUNT_COLUMN_QUERIES: dict[str, str] = {
    "gitcode_user": "um_account_user_id_by_gitcode_user",
    "github_login": "um_account_user_id_by_github_login",
}

#: managed_repos 的「仓库级 bot token 列 → 具名查询」白名单（token_resolver 的 legacy
#: 列回落分支）。只有 gitcode/github 有物理列（gitee/gitlab 的平台卡显式置空
#: token_storage_field，调用方在进 SQL 前就已返回）；未登记列抛 KeyError，
#: 由调用方原有 except 捕获后返回空串——与原「拼出不存在的列 → SQL 报错 → 空串」同口径。
_REPO_TOKEN_COLUMN_QUERIES: dict[str, str] = {
    "bot_gitcode_token": "repo_bot_token_legacy_gitcode",
    "bot_github_token": "repo_bot_token_legacy_github",
}

#: login_whitelist 老列兜底（存量列式部署的 `<provider>_username` 物理列）。
#: 列名由本域从 provider 派生并过严格标识符白名单（`[a-z0-9_]`，≤32 字符，与
#: `provider VARCHAR(32)` 同口径）——调用方的 provider 字符串**永不直接拼进 SQL**。
#: 物理列不存在（新装 / 该渠道无老列）与「该渠道无老数据」同口径：返回 False。
_LOGIN_WL_LEGACY_COLUMN_SQL = "SELECT 1 FROM login_whitelist WHERE LOWER({col})=LOWER(?)"
_LOGIN_WL_PROVIDER_RE = re.compile(r"[a-z0-9_]{1,32}")


def _any_sql(name: str) -> str:
    """取具名查询 SQL；未登记抛 KeyError（不静默退回任意 SQL）。"""
    sql = _ANY_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown portable mgmt query: {name}")
    return sql


def read_one_any(name: str, params: tuple = ()) -> Any | None:
    """按具名查询读单行（任意拓扑，可移植方言）。"""
    with _any_cursor() as c:
        c.execute(_any_sql(name), params)
        return c.fetchone()


def read_all_any(name: str, params: tuple = ()) -> list[Any]:
    """按具名查询读多行（任意拓扑，可移植方言）。"""
    with _any_cursor() as c:
        c.execute(_any_sql(name), params)
        return list(c.fetchall() or [])


def exec_any(name: str, params: tuple = ()) -> int:
    """按具名查询执行写语句（任意拓扑，自提交），返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql(name), params)
        return int(getattr(cur, "rowcount", 0) or 0)


_PLATFORM_CARD_COLUMNS = (
    "provider", "name", "dialect", "host", "api_base", "api_auth", "webhook_path",
    "event_header", "webhook_verify", "webhook_dialect", "pr_head_filter", "clone_auth",
    "issue_write_form", "branch_create_form", "issue_search", "git_tree_family",
    "pr_comment_endpoint", "pr_labels_via_issues", "workflows_write", "legacy_token_symbol",
    "pat_create_url", "token_storage_field", "webhook_token_field", "bot_username",
    "bot_token_status", "bot_checked_at", "owner_fallback", "download_base",
    "user_images_base", "brand_color",
)


def repo_platform_cards(enabled_only: bool = True) -> list[dict]:
    """D109 平台卡行（grape_t_repo_platforms 归 mgmt 域；任意拓扑）。

    runtime（vcs/platform_card）与 insight 需要平台方言/端点描述时走本原语：
    - 拆库拓扑：`read_all_any` 走 mgmt 自有 DSN（lib），读到的才是平台卡真源；
    - 单库/测试拓扑：回落进程主库，行为与拆库前一致。

    返回列见 `_PLATFORM_CARD_COLUMNS`；**凭据列不出域**（bot_token_enc / client_*_enc
    留在 mgmt，取 token 走凭据域原语）。
    """
    rows = read_all_any("repo_platform_cards" if enabled_only else "repo_platform_cards_all")
    return [dict(zip(_PLATFORM_CARD_COLUMNS, tuple(r))) for r in rows]


def mark_repo_platform_bot_status(provider: str, token_status: str,
                                  checked_at: str = "") -> int:
    """把 bot 巡检状态写回平台行（真源；D110 后 bot 归平台行）。

    幂等：同值重复写只更新时间戳；`checked_at` 为空取当前 UTC。
    任意拓扑走 `exec_any`（自有 DSN 直接写 mgmt 库；单库写进程主库）。
    """
    from datetime import datetime, timezone
    now = (checked_at or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return int(exec_any("repo_platform_bot_status_set",
                        (str(token_status or ""), now, now, str(provider or ""))) or 0)


def audit_recent_rows(limit: int, like: str = "") -> list[Any]:
    """审计流水最近 N 条，列序 `(id, event_type, user_id, operator_id, ip, created_at, detail)`。

    insight 统一事件视图的 `audit` 来源（**仅 L2**）原先在 insight 进程主库直发 SQL：
    三库拓扑下那张表在 mgmt 库（视图 0 → 1146），被调用方 except 吞成「审计来源恒空」。
    语句上移后两条拓扑跑同一份 SQL。

    `like` 为空 = 不带关键字谓词（等价原实现 `if q:` 的假分支）；非空时调用方**先包好
    通配符**（`%kw%`）再传入，本函数不再拼值。`limit` 按原语句进 SQL 参数（不做 Python
    侧截断，语义与 `LIMIT ?` 一致）。
    """
    n = int(limit)
    if like:
        return [tuple(r) for r in read_all_any("audit_recent_rows_q", (like, like, n))]
    return [tuple(r) for r in read_all_any("audit_recent_rows", (n,))]


def login_whitelist_count() -> int:
    """login_whitelist 行数（0 = 未配置任何条目，供「隐式启用 / fail-closed」判定）。

    原 kernel 实现读的是同一进程主库游标；三库拓扑下该表在 mgmt 库（视图 0），
    语句上移后两条拓扑跑同一份 SQL。
    """
    row = read_one_any("login_whitelist_count")
    return int(row[0] or 0) if row else 0


def login_whitelist_row_match(provider: str, login: str) -> bool:
    """行式新行命中：provider 精确 + username 大小写不敏感（LOWER 双侧）。"""
    return read_one_any("login_whitelist_row_match", (provider, login)) is not None


def login_whitelist_legacy_column(provider: str) -> str | None:
    """老列兜底的物理列名（`<provider>_username`）；provider 不合白名单 → None。

    本函数是该列名的**唯一**产生点：调用方只能传 provider key，不得自己拼列名。
    """
    key = provider or ""
    return f"{key}_username" if _LOGIN_WL_PROVIDER_RE.fullmatch(key) else None


def login_whitelist_legacy_column_match(provider: str, login: str) -> bool:
    """老列兜底命中（存量列式部署）。

    provider 不合标识符白名单 → False（等同该渠道无老数据）；
    物理列不存在（sqlite `no such column` / MySQL `unknown column`）→ False；
    其它 DB 异常**照原样上抛**，由调用方按原口径降级到文件白名单。
    """
    col = login_whitelist_legacy_column(provider)
    if not col:
        return False
    try:
        with _any_cursor() as c:
            c.execute(_LOGIN_WL_LEGACY_COLUMN_SQL.format(col=col), (login,))
            return c.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 - 列缺失是正常分支，其余上抛
        msg = str(exc).lower()
        if "no such column" in msg or "unknown column" in msg:
            return False
        raise


# ---------- baseline 知识 upsert / 全局 ref（runtime project-info 的兜底写路径）----------
# 调用方（runtime/project_info/builder._write_to_obs）原本在自己进程主库上
# 读 id → UPDATE / INSERT → 取 lastrowid → 补 ref：三库拓扑下进程库没有这两张 mgmt 表，
# 整段落进外层 except（只留 OBS + 本地文件）。这里把「表 + 事务边界」一起收到 owner 域：
# 单次 upsert 是读后写，必须原子（`_any_tx`），否则两个并发刷新会各插一行。

def upsert_baseline_knowledge_any(repo_key: str, display_name: str, description: str,
                                  file_path: str, obs_key: str, content_hash: str) -> dict[str, Any]:
    """baseline 知识 upsert（任意拓扑），返回 {"action","id","changed"}。

    定位口径与 builder 原实现逐字一致：repo_key + scope='baseline' + name + archived=0；
    命中 → 按 id 刷新（保留原 id，已建 ref 不失效）；未命中 → 新建（id 取 lastrowid）。
    **不写 org_id / source_seq**：原 INSERT 也不写，这里不顺手补列（否则两侧数据不一致）。
    """
    with _any_tx() as c:
        c.execute(_any_sql("knowledge_baseline_id_any"), (repo_key, display_name))
        row = c.fetchone()
        if row:
            kid = int(row[0])
            c.execute(_any_sql("knowledge_baseline_refresh"),
                      (obs_key, content_hash, display_name, description, kid))
            return {"action": "updated", "id": kid,
                    "changed": int(getattr(c, "rowcount", 0) or 0)}
        c.execute(_any_sql("knowledge_baseline_insert_any"),
                  (repo_key, file_path, obs_key, content_hash, display_name, description))
        return {"action": "inserted", "id": int(getattr(c, "lastrowid", 0) or 0),
                "changed": 1}


def ensure_baseline_knowledge_ref_any(knowledge_id: int) -> int:
    """幂等建立 baseline 的全局场景关联（OR IGNORE），返回 1=新建 / 0=已存在或 id 非法。

    与原语句的常量口径一致：scene_id='' / agent_type='' / issue_num=0 / enabled=1 /
    created_by='system'（空串=全部场景、全部 Agent，0=不限 Issue）。
    id 非法（None / 0）时**不发 SQL**，直接返回 0 —— 原实现在 `if kid:` 处就短路了。
    """
    kid = int(knowledge_id or 0)
    if kid <= 0:
        return 0
    with _any_tx() as c:
        c.execute(_any_sql("knowledge_baseline_ref_ensure"), (kid,))
        return int(getattr(c, "rowcount", 0) or 0)


def baseline_knowledge_files_any(repo_key: str) -> list[tuple]:
    """基线知识（scope='baseline'）的 (file_path, obs_key)，按 id 倒序（任意拓扑）。

    与 `list_baseline_knowledge_files` 的区别：那条是 `%s` 方言、只有自有 DSN 拓扑可用，
    调用方必须自带本地兜底 SQL —— 兜底 SQL 正是 runtime/engine/service 的跨域表引用来源，
    而三库拓扑下 runtime 进程库没有 agent_shared_knowledge（视图 0 → 1146，被外层
    `except` 吞成「project-info 为空」）。这里两条拓扑同一份可移植语句。
    空 repo_key 不发 SQL 直接返回 []（与原查询的匹配结果等价）。
    """
    if not repo_key:
        return []
    return [tuple(r) for r in read_all_any("knowledge_baseline_files", (repo_key,))]


def has_manual_knowledge_ref(repo_key: str, agent_type: str = "", issue_num: int = 0,
                             scene_id: str = "") -> bool:
    """是否有关联到该（仓库/场景/agent/issue）的手工沉淀知识（source='manual'）。

    报 row 存在：scoped 列为空串或 0 视为通配（与 kernel 原 SQL 的
    `col = ? OR col = ''` 口径一致），只认 enabled=1 + accepted + 未归档。
    """
    row = read_one_any("knowledge_ref_manual_exists",
                       (scene_id, agent_type, issue_num, repo_key))
    return bool(row and row[0])


def list_agent_knowledge_refs(repo_key: str, agent_type: str = "", issue_num: int = 0,
                              scene_id: str = "") -> list[tuple]:
    """agent 沉淀知识关联行 (id, scope, source, obs_key, file_path, content_hash)。

    列序与 kernel 原查询逐字一致（调用方按下标解包）；去重口径 DISTINCT 保留。
    """
    return [tuple(r) for r in read_all_any(
        "knowledge_ref_agent_rows", (scene_id, agent_type, issue_num, repo_key))]


def repo_dev_scene(repo_key: str) -> str:
    """仓库默认场景 id（managed_repos.dev_scene）；无行/空值返回空串。"""
    row = read_one_any("repo_dev_scene", (repo_key,))
    return str(row[0] or "") if row else ""


# ---------- 渠道安装表的解绑/回显原语（runtime/routes/chat.py；任意拓扑） ----------
#
# 原实现在 chat.py 里用 `engine_wiring._connect(default_db_path())` 拿 runtime 库句柄直发
# 这三条 SQL：单库拓扑能跑，三库拆分后 runtime 库没有 grape_t_channel_installation（视图 0）
# → 1703 的持有者校验直接 1146（保存 IM 配置 500），另两条被 except 吞成「无引用 / 无安装」
# （app 释放误清他人映射、前端多渠表单恒「未接入」）。SQL 上移到本域后两条拓扑同源。

def installation_other_refs_any(app_id: str, owner_user_id: str) -> int:
    """该 app 的 active 安装里，除 (scope=user, owner 本人) 这条外还剩几条引用。"""
    if not app_id:
        return 0
    row = read_one_any("installation_other_refs_count", (app_id, str(owner_user_id)))
    return int(row[0] or 0) if row else 0


def installation_holders_any(app_id: str) -> list[tuple]:
    """该 app 的全部 active 持有行 (scope, owner_user_id)；无行返回空列表。"""
    if not app_id:
        return []
    return [tuple(r) for r in read_all_any("installation_holders_by_app", (app_id,))]


def installation_user_channel_rows_any(owner_user_id: str) -> list[tuple]:
    """某用户各渠道的安装行 (channel_type, status, config)，按 updated_at 倒序。

    口径照抄原查询：只认 active/disabled 两态（revoked 不参与回显），不做去重，
    「同渠道取最新一条」由调用方按首见保留（原实现同）。
    """
    return [tuple(r) for r in read_all_any("installation_user_channels", (str(owner_user_id),))]


def clear_user_mapping_feishu_any(app_id: str = "", user_id: str = "", updated_at: str = "") -> int:
    """清空飞书映射（app 释放按 app_id / 精确按 user_id），返回变更行数。

    与 `clear_user_mapping_feishu` 的区别：那条是 `%s` 方言、只在自有 DSN 拓扑可用，
    调用方因此保留了本地兜底 SQL（跨域统计来源）；本函数走 `?` 具名查询，两条拓扑同源。
    updated_at 由调用方算出传参（原实现用 Python 侧 `datetime.now().isoformat()`）。
    """
    if app_id:
        return exec_any("um_clear_feishu_by_app", (updated_at, app_id))
    if not user_id:
        raise ValueError("app_id / user_id 至少给一个")
    return exec_any("um_clear_feishu_by_user", (updated_at, str(user_id)))


def delete_feishu_onboard_task_any(app_id: str) -> int:
    """删除该 app 的飞书扫码接入任务（任意拓扑；空 app_id 不落到 `WHERE app_id=''`）。"""
    if not app_id:
        return 0
    return exec_any("onboard_task_delete_by_app", (app_id,))


# ---------- IM 身份解析 / 巡检执行卡（runtime/im/handler_async；任意拓扑） ----------
#
# 原实现在 runtime 进程里用进程主库游标直发这四条 SQL：单库拓扑能跑，三库拆分后
# 渠道安装 / 渠道身份 / 巡检任务 / 流水线场景四表只在 mgmt 库（视图 0）→ 1146 被内部
# except 吞成「查不到用户 / 无场景」，表现为钉钉等新引擎渠道消息 uid=0（名下没有仓库）、
# 巡检卡片的场景下拉恒空。身份反查的两表 JOIN 不跨域（同属本域），仍留一条具名查询。


def sender_identity_user_by_app_any(app_id: str, sender_id: str) -> str:
    """按 (app, 平台用户) 取发送者绑定用户 id（任意拓扑）；无行/参数空返回空串。"""
    if not app_id or not sender_id:
        return ""
    row = read_one_any("sender_identity_user_by_app", (app_id, str(sender_id)))
    return str(row[0]) if row else ""


def installation_owner_by_app_any(app_id: str) -> str:
    """按 app 取 user 轨安装的 owner_user_id（单用户 bot 兜底）；无行返回空串。"""
    if not app_id:
        return ""
    row = read_one_any("installation_owner_by_app_user_scope", (app_id,))
    return str(row[0]) if row else ""


def autopilot_task_brief_any(task_id) -> tuple | None:
    """取巡检任务的 (repo_key, name)（任意拓扑）；不存在返回 None。"""
    row = read_one_any("autopilot_task_brief_row", (int(task_id),))
    return tuple(row) if row else None


def scene_names_by_repo_any(repo_key: str) -> list[tuple]:
    """仓库下在用场景的 (scene_id, name) 列表，按 name 排序；无行返回空列表。"""
    return [tuple(r) for r in read_all_any("scene_names_by_repo", (repo_key or "",))]


# ---------- 巡检任务调度（runtime/engine/scheduler.py；任意拓扑） ----------
#
# 任务表与订阅表都归本域。原实现四处读 + 一处写全是「mgmt_lib 优先 / 进程主库兜底 SQL」
# 双分支：兜底 SQL 正是跨域引用来源，而三库拓扑下 runtime 进程库没有 autopilot_* 表
# （视图 0 → 1146，被调度循环的 except 吞成「无任务 / 无订阅者 / 无 last_run」）。
# 现统一到任意拓扑原语：自有 DSN → 本域连接，单库 → 进程主库 + db_lock，SQL 只留本域一份。
# 订阅 + 任务的 JOIN 两表同属本域（不是跨域 JOIN），整条保留；列序与原语句逐字一致。


def autopilot_subscribers_any(task_id) -> list[tuple]:
    """任务订阅者 (user_id, name, repo_key)；无订阅返回空列表。"""
    return [tuple(r) for r in read_all_any("autopilot_subscribers_by_task", (int(task_id),))]


def list_enabled_autopilot_tasks_any() -> list[tuple]:
    """启用中的自动化任务全列（列序与调度器原查询逐字一致，调用方按下标解包）。"""
    return [tuple(r) for r in read_all_any("autopilot_tasks_enabled_full", ())]


def autopilot_task_row_any(task_id) -> tuple | None:
    """单条任务全列（列序与调度器触发路径原查询逐字一致）；不存在返回 None。"""
    row = read_one_any("autopilot_task_full_row", (int(task_id),))
    return tuple(row) if row else None


def autopilot_task_last_run_any(task_id) -> tuple | None:
    """任务最近运行时间行 (last_run_at,)；无行返回 None（调用方 `row[0] if row else ''`）。"""
    return read_one_any("autopilot_task_last_run", (int(task_id),))


def set_autopilot_run_info_any(task_id, last_run_at: str, next_run_at: str) -> int:
    """回写 last_run_at / next_run_at / updated_at（updated_at 与原实现一致 = last_run_at）。"""
    return exec_any("autopilot_run_info_set",
                    (last_run_at, next_run_at, last_run_at, int(task_id)))


# ---------- 仓库级 bot token / 登录名（kernel/token_resolver；任意拓扑） ----------
#
# 第 2 级策略（RepoBotToken）的四处读写 + 回帖链的 actor 归属判定。原实现全部在
# kernel 里用进程主库游标直发 SQL：单库拓扑能跑，三库拓扑下 managed_repos 只在 mgmt
# 库 → 1146（被内部 except 吞成「无凭据」，策略链静默降级到全局 bot 或用户 token），
# 同表副本存在时则按副本判定（过期标记/登录名与实际不一致）。列序与谓词照抄原语句。


def repo_bot_token_from_legacy_column_any(repo_key: str, column: str) -> str:
    """读 managed_repos 的 legacy bot token 列（密文原值）；列名走本域白名单。

    返回原值（未解密）——解密在调用方（credential_crypto 属 mgmt auth，由调用方函数内
    导入，读侧密钥口径不变）。空行/空值返回空串。未登记列抛 KeyError。
    """
    name = _REPO_TOKEN_COLUMN_QUERIES.get(column)
    if name is None:
        raise KeyError(f"unsupported managed_repos token column: {column}")
    row = read_one_any(name, (repo_key,))
    return str(row[0]) if row and row[0] else ""


def repo_bot_token_status_any(repo_key: str) -> str:
    """仓库级 bot token 状态列（bot_token_status）；无行/空值返回空串。

    调用方原口径是「行存在且值 == 'expired'」，空串与无行等价（都不会短路执行链）。
    """
    row = read_one_any("repo_bot_token_status", (repo_key,))
    return str(row[0] or "") if row else ""


def set_repo_bot_token_status_any(repo_key: str, status: str) -> int:
    """置仓库级 bot token 状态（'expired' 或 ''），返回影响行数。

    原实现是两条字面量语句（mark_expired / clear_expired），此处合并成一条参数化
    UPDATE：唯一可变的是状态值，谓词与 rowcount 语义（仓库不存在 → 0）不变。
    """
    return exec_any("repo_bot_token_status_set", (status or "", repo_key))


def stored_access_token_any(grape_user_id: Any, provider: str) -> str:
    """external_identities 的 access_token 密文原值（status IN (1, 4)）。

    供 token_resolver.read_stored_token 使用：不触发 OAuth 刷新、只读一列，
    解密与过期判定都留在调用方（口径不变）。
    """
    row = read_one_any("ei_stored_access_token", (str(grape_user_id), provider or ""))
    return str(row[0]) if row and row[0] else ""


def oauth_token_status_any(grape_user_id: Any, provider: str) -> tuple | None:
    """external_identities 的 (access_token, status) 原值（status IN (1, 4)）。

    只交出密文与状态列，解密与「过期仍返回 token」的口径留在调用方（逐字照抄原语句，
    无 LIMIT：原实现 fetchone 取首行，与 LIMIT 1 不同行序多行的取舍保持一致）。
    """
    return read_one_any("ei_oauth_token_status", (str(grape_user_id), provider or ""))


def token_expires_at_any(grape_user_id: Any, provider: str) -> Any | None:
    """external_identities.token_expires_at 原值（status IN (1, 4)）；无行返回 None。

    时间字符串的解析（UTC 口径 / 解析失败=0）留在调用方，本域不做时间语义判定。
    """
    row = read_one_any("ei_token_expires_at", (str(grape_user_id), provider or ""))
    return row[0] if row else None


def mark_external_identity_expired_any(grape_user_id: Any, provider: str) -> int:
    """凭据失效标记（任意拓扑）：external_identities.status 1 → 4，返回影响行数。

    与同模块 `mark_external_identity_expired`（`db_registry.cursor("mgmt")` + `%s`，
    供 outbox drain / mgmt 进程用）语义逐字一致，区别只在拓扑：自有 DSN 走 mgmt 连接，
    单库/测试拓扑回落进程主库，所以 runtime 侧不必再留一份本地兜底语句。

    只改 status=1 的行（已是 4 的行不改，重复标记 rowcount=0）；不做「凭据是否真失效」
    的判定，那是调用方 401/invalid_grant 口径。
    """
    uid = "" if grape_user_id is None else str(grape_user_id)
    return exec_any("ei_expire_mark", (uid, provider or ""))


# ---------- 事件知识清理 / 账户名（runtime/routes/events.py 调用；任意拓扑） ----------
#
# 原实现每个站点都是「mgmt_lib 优先 + 本地兜底 SQL」：兜底只在单库/视图期执行，
# 却是跨域表引用的来源，且自有 DSN 拓扑下进程库没有该表（无视图 → 1146）。
# 兜底侧上移成 owner 域原语后，两条拓扑同一份语句；调用方不再保留任何外域 SQL。
# 删除顺序与原实现一致（先按 knowledge_id 删 ref，再删主表），并在同一事务里完成。


#: scene_knowledge_ref 的 IN 列表分块大小（MySQL/sqlite 参数上限的保守值）。
_KNOWLEDGE_REF_DELETE_CHUNK = 400


def knowledge_obs_keys_scoped(repo_key: str, scope: str, agent_type: str,
                              issue_num: Any) -> list[str]:
    """按（仓库/scope/agent/issue）取非空 obs_key（事件清理删对象用）。

    issue_num 按调用方原值绑定（原 SQL 就是这么绑的；gitee 的 issue 号可能是非数字串，
    这里不强制 int，避免把可跑的站点点成 500）。
    """
    rows = read_all_any("knowledge_obs_keys_scoped", (repo_key, scope, agent_type, issue_num))
    return [str(r[0]) for r in rows if r and r[0]]


def knowledge_ids_scoped(repo_key: str, scope: str, agent_type: str,
                         issue_num: Any) -> list[int]:
    """按（仓库/scope/agent/issue）取知识主键 id 列表（任意拓扑）；issue_num 原值绑定。"""
    rows = read_all_any("knowledge_ids_scoped_portable", (repo_key, scope, agent_type, issue_num))
    return [int(r[0]) for r in rows if r and r[0] is not None]


def delete_event_knowledge_any(repo_key: str, scope: str, agent_type: str,
                               issue_num: Any) -> dict[str, int]:
    """删除某事件的知识记录（任意拓扑，单事务）：先删 ref 再删主表。

    与 `delete_event_knowledge`（自有 DSN / MySQL 方言）同语义，供单库与
    自有 DSN 两条拓扑共用；返回口径一致（knowledge / refs_deleted / rows_deleted）。
    issue_num 按原值绑定（不强制 int，gitee 的 issue 号可能是非数字串）。
    """
    params = (repo_key, scope, agent_type, issue_num)
    refs_deleted = 0
    with _any_tx() as c:
        c.execute(_any_sql("knowledge_ids_scoped_portable"), params)
        kids = [int(r[0]) for r in (c.fetchall() or []) if r and r[0] is not None]
        for start in range(0, len(kids), _KNOWLEDGE_REF_DELETE_CHUNK):
            chunk = kids[start:start + _KNOWLEDGE_REF_DELETE_CHUNK]
            ph = ",".join(["?"] * len(chunk))
            cur = c.execute(
                f"DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ({ph})",
                tuple(chunk))
            refs_deleted += int(getattr(cur, "rowcount", 0) or 0)
        cur = c.execute(_any_sql("knowledge_delete_scoped"), params)
        rows_deleted = int(getattr(cur, "rowcount", 0) or 0)
    return {"knowledge": len(kids), "refs_deleted": refs_deleted,
            "rows_deleted": rows_deleted}


def account_username_by_id_any(account_id: Any) -> str:
    """按账户 id 取 username（任意拓扑）；id 为空/账户不存在返回空串。

    供外域把 `owner_name = (SELECT username FROM grape_accounts WHERE id=?)`
    这类跨域子查询拆成「本域取标量 + 参数化比对」（原 SQL 子查询为空时恒不匹配，
    这里同样以空串表示「无此账户」，由调用方决定是否生成该过滤分支）。
    """
    if not account_id:
        return ""
    row = read_one_any("account_username_by_id", (str(account_id),))
    return str(row[0] or "") if row else ""


def user_account_username_by_id_any(user_id: Any) -> str:
    """按 id 取**个人账号** `user_accounts.username`（任意拓扑）；无行 / id 非法 → 空串。

    与 `account_username_by_id_any` 的区别：那是登录账号 `grape_accounts`，这是个人账号
    `user_accounts`，同 id 空间但两张表，不能互相顶替。

    `id` 非法（0 / 非数字）→ 空串且**不查库**：原调用点（runtime/pipeline/account_env
    的 autopilot 任务创建者解析）在 `created_by=0` 的历史任务上正是这么短路的 —— 一次
    无意义查询换不来任何信息。id 按整数绑定：两边后端的该列都是 INTEGER 主键，
    传字符串在 MySQL 侧会走隐式转换、在 SQLite 侧直接匹配不到。

    没有 is_disabled / is_active 过滤：恢复的是原语句逐字口径（已停用账号也要回原名，
    是否采用由调用方的账号形态校验决定）。
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return ""
    if not uid:
        return ""
    row = read_one_any("user_account_username_by_id", (uid,))
    return str(row[0] or "") if row else ""


def account_id_by_username_valid_any(username: str, cursor: Any = None) -> Any | None:
    """用户名 → 有效账号 id（任意拓扑，大小写不敏感）；无此账号返回 None。

    `valid` 口径：id 非空且不是历史 int 主键的 `'0'` 哨兵 —— 原 `grape_accounts_0b3fdc`
    兜底 SQL 的同一过滤（供「引擎 owner_name 是否真的指向某个用户」的判定使用）。
    `cursor` 非空且单库/测试拓扑 → 复用调用方游标（不嵌套 db_lock、保住调用方事务快照）；
    自有 DSN → 忽略它，走 mgmt 连接（那正是原来 1146 的来源）。
    """
    if not username:
        return None
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("account_id_by_username_valid"), (username,))
        row = c.fetchone()
    return row[0] if row else None


def account_id_by_username_ci_any(username: str, cursor: Any = None) -> Any | None:
    """用户名 → 账号 id（任意拓扑，LOWER 双侧）；无此账号返回 None。

    逐字照抄原 `grape_accounts_d0ac07`：不加 LIMIT、不过滤 id 哨兵（用户名理论上唯一，
    首行即答案；收紧会改变 claim_by_tag 的 user scope 过滤结果）。`cursor` 语义同上。
    """
    if not username:
        return None
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("account_id_by_username_ci"), (username,))
        row = c.fetchone()
    return row[0] if row else None


def account_id_by_username_first_any(username: str, cursor: Any = None) -> str | None:
    """用户名 → 账号 id（任意拓扑，LOWER 双侧 + `LIMIT 1`）；无此账号返回 ``None``。

    逐字照抄 runtime 注册回填的兜底句 `SELECT id FROM grape_accounts WHERE
    LOWER(username)=LOWER(?) LIMIT 1`（`routes/runtime.py` 的 D32 补丁：user scope
    凭据注册时把 `owner_id` 对齐到 `grape_accounts.id`）。原实现是「mgmt lib →
    foreign-only lib → 进程主库直发」三段兜底，后两段在三库拓扑下必 1146（进程库
    没有该表、视图 0），整段被外层 `except` 吞成「owner_id 保持凭据行原值」——
    个人引擎的 scope 判定因此按错 owner 落库。上移后两条拓扑跑同一份语句。

    与 `account_id_by_username_ci_any` 的区别只有 `LIMIT 1`（原语句自带，别合）。
    返回口径按 `get_account_id_by_username`（lib 侧主路径）：**id 为空/`None` 视为
    未命中**返回 ``None``，由调用方决定回退值（不把空串当账号写进 owner_id）。
    `cursor` 语义同 `account_id_by_username_ci_any`：单库 / 测试拓扑复用它（调用方
    库即本域库），自有 DSN 忽略、走 mgmt 连接。
    """
    if not username:
        return None
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("account_id_by_username_ci_limit1"), (username,))
        row = c.fetchone()
    if not row or row[0] in (None, ""):
        return None
    return str(row[0])


# ---------- 代发内容台账（kernel/outbound_ledger 调用；任意拓扑） ----------
#
# outbound_content_ledger 归 mgmt。kernel 原实现「建表 + 6 条读写语句」全打在进程主库上：
# 单库拓扑能跑；三库拓扑下进程库里没有该表（视图 0）→ 1146，写侧被 except 吞成 False
# （记账只剩进程内 TTL 集合，多进程/重启/超过 300s 的回声全部漏拦），读侧被吞成「放行」
# （用户消息被误当自己的回声拦掉的概率反而没了，但防线也等于不存在），
# 且同表副本存在时静默读写副本。现按「表归谁、SQL 归谁」把结构与读写全部下沉本域：
# kernel 只留行为（TTL 集合、指纹、时效窗口、放行策略），一条 SQL 都不剩。
#
# 方言：`?` 占位符、`INSERT OR IGNORE`（MySQL 桥翻译为 INSERT IGNORE）、
# `datetime('now')` 一律不用——截止时间由调用方在 Python 侧算成 '%Y-%m-%d %H:%M:%S' 传参。

OUTBOUND_LEDGER_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS outbound_content_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        content_type TEXT DEFAULT '',
        content_id TEXT DEFAULT '',
        issue_number TEXT DEFAULT '',
        actor_identity TEXT DEFAULT '',
        origin_channel TEXT DEFAULT 'webhook',
        origin_task TEXT DEFAULT '',
        content_hash TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )
"""


def outbound_ledger_ddl() -> str:
    """outbound_content_ledger 建表 DDL（只读快照，供文档/测试比对）。"""
    return OUTBOUND_LEDGER_TABLE_SQL


def ensure_outbound_ledger_table() -> None:
    """幂等引导 outbound_content_ledger（建表 + 两个索引 + 旧索引列数迁移）。

    步骤与容错口径与 kernel 原 `_ensure_table` 逐条等价：
      1) CREATE TABLE IF NOT EXISTS（失败抛出——建表失败说明连接/权限有问题，与原实现一致）；
      2) 唯一索引 (provider, repo_key, content_type, content_id)：漏建会让
         INSERT OR IGNORE 失去去重语义（重复记账 → 幻觉命中），故失败只告警不阻断；
      3) 旧 3 列唯一索引迁移：该索引不含 repo_key，同 ID 跨仓会被误判重复。
         列数探测用 information_schema（MySQL 专有）：sqlite / 无该目录时探测失败即跳过，
         `CREATE ... IF NOT EXISTS` 已覆盖新库，行为与原实现一致；
      4) 指纹索引 (provider, content_hash)：读侧旁路核对用。

    连接按拓扑自适应（自有 DSN → mgmt；单库 → 进程主库并持 db_lock、末尾提交）。
    本函数自身不做「一次性」缓存：调用方（kernel）按连接对象缓存，避免热路径重复往返。
    """
    with _any_cursor(write=True) as c:
        c.execute(OUTBOUND_LEDGER_TABLE_SQL)
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_ledger_cid "
                      "ON outbound_content_ledger (provider, repo_key, content_type, content_id)")
        except Exception as exc:  # noqa: BLE001 - 索引已存在/方言差异不阻断记账
            log.debug(f"outbound ledger unique index skipped: {exc}")
        try:
            c.execute("SELECT COUNT(DISTINCT COLUMN_NAME) FROM information_schema.statistics "
                      "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='outbound_content_ledger' "
                      "AND INDEX_NAME='idx_outbound_ledger_cid'")
            _row = c.fetchone()
            _ncols = int(_row[0]) if _row and _row[0] is not None else 0
            if _ncols and _ncols < 4:
                c.execute("DROP INDEX idx_outbound_ledger_cid ON outbound_content_ledger")
                c.execute("CREATE UNIQUE INDEX idx_outbound_ledger_cid "
                          "ON outbound_content_ledger "
                          "(provider, repo_key, content_type, content_id)")
        except Exception as exc:  # noqa: BLE001 - sqlite/无 information_schema：跳过迁移
            log.debug(f"outbound ledger index migration skipped: {exc}")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_outbound_ledger_hash "
                      "ON outbound_content_ledger (provider, content_hash)")
        except Exception as exc:  # noqa: BLE001 - 索引已存在不阻断记账
            log.debug(f"outbound ledger hash index skipped: {exc}")


def record_outbound_ledger(provider: str, repo_key: str, content_type: str, content_id: str,
                           issue_number: str = "", actor_identity: str = "",
                           origin_channel: str = "webhook", origin_task: str = "",
                           content_hash: str = "", created_at: str = "") -> None:
    """记一条代发台账（INSERT OR IGNORE：同键重复记账静默丢弃，幂等）。

    只落库，不判断入账前提（provider/repo/type/content_id 非空与 TTL 集合都在调用方，
    口径不变）。created_at 由调用方给（调用方原语义就是 Python 侧 time.strftime）。
    失败向上抛——调用方的 best-effort 包装负责降级，避免本层静默吞异常。
    """
    exec_any("outbound_ledger_insert",
             (provider, repo_key, content_type, content_id, issue_number, actor_identity,
              origin_channel, origin_task, content_hash, created_at))


def purge_outbound_ledger_before(cutoff: str) -> int:
    """删除 created_at 早于 cutoff 的记录（保留期清理），返回删除行数。"""
    return exec_any("outbound_ledger_purge_before", (cutoff,))


def outbound_ledger_has_edit(provider: str, repo_key: str, content_type: str,
                             content_id: str, content_hash: str) -> bool:
    """issue_edit 回声判定：ID（issue 编号，共享键）+ 指纹双命中才算命中。"""
    row = read_one_any("outbound_ledger_exists_edit",
                       (provider, repo_key, content_type, content_id, content_hash))
    return bool(row)


def outbound_ledger_has_within(provider: str, repo_key: str, content_type: str,
                               content_id: str, cutoff: str) -> bool:
    """issue_close / issue_meta 回声判定：编号命中 + 记录在时效窗口内（created_at>=cutoff）。"""
    row = read_one_any("outbound_ledger_exists_within",
                       (provider, repo_key, content_type, content_id, cutoff))
    return bool(row)


def outbound_ledger_has_id(provider: str, repo_key: str, content_type: str,
                           content_id: str) -> bool:
    """精确 ID 命中（评论/issue/PR 族的主判定）。"""
    row = read_one_any("outbound_ledger_exists_id",
                       (provider, repo_key, content_type, content_id))
    return bool(row)


def outbound_ledger_row_by_hash(provider: str, content_hash: str, repo_key: str):
    """按内容指纹反查台账行 (content_id,)——仅供调用方做旁路核对告警，不参与拦截判定。

    返回原始行（无行 → None）：调用方原口径是 `if _row:` 再取值打日志，
    空 content_id 的行也会触发告警，这里保持同一形态。
    """
    return read_one_any("outbound_ledger_row_by_hash", (provider, content_hash, repo_key))


# ---------- 飞书通道列（runtime/im/channel_directory 调用） ----------
#
# channel_directory 原先每个站点都是「mgmt_lib 优先 + 本地兜底 SQL」：兜底侧只在
# 单库/视图期执行，却是跨域表引用的来源，且三库拓扑下进程库没有该表（无视图 → 1146）。
# 这里把兜底侧上移成 owner 域原语：SQL 只留 mgmt 一份，两条拓扑同一份语句。
# 写语义逐条照抄 channel_directory 原语句（datetime('now') / USER_ID>0 守卫 /
# app 级与 user 级清空的作用域 WHERE 都不改）。


def write_back_feishu_identity(user_id: Any, open_id: str, feishu_name: str = "",
                               app_id: str = "") -> None:
    """agent 引导回写飞书三列（FR-18.4：USER_ID 必须 >0，禁 0 占位行）。

    建行（INSERT OR IGNORE）+ 带 USER_ID>0 守卫的 UPDATE，同一事务；
    app_id 可为空串但由调用方显式传（app 级成对语义留在调用方）。
    """
    with _any_tx() as c:
        c.execute(_any_sql("um_insert_ignore_user"), (user_id,))
        c.execute(_any_sql("um_set_feishu_by_user"),
                  (open_id, feishu_name, app_id, user_id))


def feishu_open_id_by_user(user_id: Any) -> str:
    """按 user_id 取 open_id（空值/无行返回空串，与原 `row[0] if row else ''` 同口径）。"""
    row = read_one_any("um_feishu_open_id_by_user", (user_id,))
    return str(row[0] or "") if row else ""


def user_mapping_row_by_open_id(open_id: str) -> tuple | None:
    """open_id → (user_id, gitcode_user, feishu_name, gitcode_nickname)；无行返回 None。"""
    row = read_one_any("um_row_by_feishu_open_id", (open_id,))
    return tuple(row) if row else None


def feishu_open_id_by_platform_column(column: str, login: str) -> str:
    """um 名字域兜底（FR-CQ-1）：按平台列取 open_id；未登记列返回空串。

    未登记列（D39/S59 无 um 列平台）与「拼出空列名 → SQL 报错 → 整函数返回空」
    原口径一致，只补一条 debug 日志；查询异常同样返回空串（原实现吞掉）。
    """
    name = _UM_OPENID_COLUMN_QUERIES.get(column or "")
    if name is None:
        log.debug(f"feishu_open_id_by_platform_column: column not registered ({column!r})")
        return ""
    try:
        row = read_one_any(name, (login,))
    except Exception as exc:  # noqa: BLE001 - 原实现同样吞掉 SQL 错误并返回空串
        log.debug(f"feishu_open_id_by_platform_column: query failed ({name}): {exc}")
        return ""
    return str(row[0] or "") if row else ""


def feishu_name_by_platform_column(column: str, login: str) -> str:
    """um 平台列 → 飞书昵称（FR-17.1 分列查，防跨平台同名误命中）；未登记列返回空串。

    未登记列（D39/S59 无 um 列平台）与原「拼出空列名 → SQL 语法错 → 整函数返回空」
    同口径；查询异常同样返回空串（原实现吞掉）。空串的语义是「无昵称」，由调用方
    决定回退到平台用户名——本层不替调用方兜底文案。
    """
    name = _UM_FEISHU_NAME_COLUMN_QUERIES.get(column or "")
    if name is None:
        log.debug(f"feishu_name_by_platform_column: column not registered ({column!r})")
        return ""
    try:
        row = read_one_any(name, (login,))
    except Exception as exc:  # noqa: BLE001 - 原实现同样吞掉 SQL 错误并回退用户名
        log.debug(f"feishu_name_by_platform_column: query failed ({name}): {exc}")
        return ""
    return str(row[0] or "") if row else ""


def clear_feishu_by_app(app_id: str, updated_at: str = "") -> int:
    """app 释放：清空该 app 全部用户的飞书三列（WHERE app_id=? 作用域必须保留）。"""
    if not app_id:
        return 0
    from datetime import datetime

    return exec_any("um_clear_feishu_by_app",
                    (updated_at or datetime.now().isoformat(), app_id))


def clear_feishu_by_user(user_id: Any, updated_at: str = "") -> int:
    """user 级精确清空（FR-21.10：按 user_id，不按用户名扫列，防误清同名用户）。"""
    if not user_id:
        return 0
    from datetime import datetime

    return exec_any("um_clear_feishu_by_user",
                    (updated_at or datetime.now().isoformat(), user_id))


def set_gitcode_nickname_any(user_id: Any, nickname: str = "") -> None:
    """昵称更新：无行先建行（INSERT OR IGNORE）再写昵称，同一事务（原两语句同批口径）。"""
    with _any_tx() as c:
        c.execute(_any_sql("um_insert_ignore_user"), (user_id,))
        c.execute(_any_sql("um_set_nickname_by_user"), (nickname or "", user_id))


# ---------- 流水线场景 / 托管仓库（runtime 事件管道） ----------
#
# 原实现每个站点都是「mgmt_lib 优先 + 本地兜底 SQL」双份：lib 句柄只在独立拓扑下可用，
# 兜底 SQL 正是跨域表引用的来源，而三库拓扑下进程库根本没有这些表（无视图 → 1146）。
# SQL 上移到本域后调用方只剩业务规则，两条拓扑跑同一份语句（自有 DSN → mgmt 连接；
# 单库/测试 → 进程主库）。列序与调用方原查询逐字一致，按下标取值不用改。


def scene_nodes_any(repo_key: str, scene_id: str) -> Any | None:
    """场景节点 JSON（按仓库+场景，**不过滤 is_active**：QA skill 账面口径）。

    无行返回 None；行存在但 nodes_json 为空时返回空串（调用方 `or "[]"` 后同口径）。
    """
    row = read_one_any("scene_nodes_by_repo_scene", (repo_key, scene_id))
    return row[0] if row else None


def scene_nodes_row_by_id_any(row_id: int, cursor: Any = None) -> Any | None:
    """按行号取场景节点行 `(nodes_json,)`：无行返回 None（行存在但值为 NULL 时返回 `(None,)`）。

    对应 runtime `recovery._readd_scene_bindings` 的按 id 读节点。原实现是双份：
    `mgmt_lib(foreign_only=True).read_one("devops_pipeline_scene_b5cd89", ...)` 优先，
    不可用时（单库拓扑 / mgmt 进程内）在**调用方游标**上直发本表 —— 那句兜底 SQL 正是
    跨域表引用的来源，且三库拓扑下 runtime 进程库没有本表（视图 0 → 1146），加回引擎
    重建节点绑定会静默失败。语句上移后两条拓扑跑同一份 SQL。

    语义逐字照抄：
      - 只按主键定位，**不加 `is_active=1`**：解绑审计指向的场景可能已停用，多一个谓词
        等于改了命中集合；
      - 返回整行而不是首列，调用方要区分「无行」（标 rebound 跳过）与「行在、nodes_json
        为 NULL/空串」（按 `or "[]"` 兜成空节点表）；
      - `cursor` 语义见 `_same_domain_read_cursor`：同库时复用（保住调用方事务快照）。
    """
    with _same_domain_read_cursor(cursor) as c:
        c.execute(_any_sql("scene_nodes_by_id"), (row_id,))
        return c.fetchone()


def scene_nodes_write_by_id(nodes_json: str, now_iso: str, row_id: int,
                            cursor: Any = None) -> int:
    """按行号覆写场景节点，返回影响行数（0 = 该行不存在）。

    对应 runtime 引擎绑定维护（`recovery._readd_scene_bindings` /
    `engine_select.cleanup|remap_scene_engine_bindings`）原有的**本地兜底句**
    （按行号覆写节点 + 时间戳，语句见本域 `_ANY_QUERIES[...]`）：
    三个调用点都只在 `mgmt_lib(foreign_only=True)` 不可用时才走它（单库拓扑，或
    mgmt 进程内的同域写），而那一跳正是跨域表引用的来源，且三库拓扑下 runtime 进程库
    没有本表（视图 0 → 1146）。

    语义逐字照抄：
      - `updated_at` 写调用方算好的 `now_iso`（**不改成 NOW()**，两条后端口径一致）；
      - `WHERE id=?` 只命中一行，不改 `is_active` / 其它列，无行时返回 0（不插入）；
      - `nodes_json` 原样落库（空串就写空串，不兜 `[]`）。
    `cursor` 语义见 `_same_domain_write_cursor`：调用方与表同库时复用它、**提交责任仍在
    调用方**（调用点后续 `db.commit()` / `rollback()` 的语义不变）；真跨域才换本域连接。
    """
    with _same_domain_write_cursor(cursor) as c:
        cur = c.execute(_any_sql("scene_nodes_write_by_id"), (nodes_json, now_iso, row_id))
        return int(getattr(cur, "rowcount", 0) or 0)


def list_pr_subscription_scenes_any(repo_key: str) -> list[tuple]:
    """启用了 PR 订阅且在用的场景 (scene_id, nodes_json, advanced_options)。"""
    return [tuple(r) for r in read_all_any("pr_subscription_scenes", (repo_key,))]


def scene_active_nodes_any(repo_key: str, scene_id: str) -> Any | None:
    """在用场景的 (scene_id, nodes_json)：无行或已停用返回 None（PR 重派判定用）。"""
    return read_one_any("scene_active_nodes_by_repo_scene", (repo_key, scene_id))


def scene_linked_repos_any(scene_id: str, repo_key: str) -> Any | None:
    """在用场景的 (linked_repos, advanced_options)：无行返回 None。"""
    return read_one_any("scene_linked_repos", (scene_id, repo_key))


def scene_linked_repos_rows_any(repo_key: str) -> list[Any]:
    """仓库下**所有在用场景**的 (linked_repos, advanced_options) 列表。

    与 `scene_linked_repos_any` 的差别：不限单个 scene_id、不取 LIMIT 1 —— A2A 关联仓
    信任域要把该仓库全部场景的关联列表都扫一遍，少一行就会漏判。
    """
    return list(read_all_any("scene_linked_repos_by_repo", (repo_key,)))


def scene_id_exists_any(scene_id: str, repo_key: str) -> str:
    """场景在该仓库下是否在用；命中返回 scene_id 原值，否则空串。"""
    row = read_one_any("scene_id_active_by_repo_scene", (scene_id, repo_key))
    return str(row[0] or "") if row else ""


def scene_active_flags_any(scene_id: str, repo_key: str) -> list[Any]:
    """该场景行（含 repo_key='' 的通用行）的 is_active 列表；无行返回空列表。"""
    return list(read_all_any("scene_active_flags", (scene_id, repo_key or "")))


def count_active_scenes_any(repo_key: str) -> int:
    """该仓库下启用中的场景数（api 首次托管判定「是否已有默认场景」）。

    只数 `is_active=1`（停用行不算已存在，与原语句一致）；仓库无场景返回 0。
    """
    row = read_one_any("scene_active_count_by_repo", (repo_key,))
    return int(row[0] or 0) if row else 0


def set_scene_deploy_url_any(deploy_url: str, scene_id: str, repo_key: str) -> int:
    """登记场景部署地址（仅在用场景行），返回影响行数；0 行=场景不存在/已停用。"""
    return exec_any("scene_deploy_url_set", (deploy_url, scene_id, repo_key))


def repo_bot_username_any(repo_key: str) -> str:
    """仓库配置的非空 bot 用户名；无行/空值返回空串（回落到平台全局 bot）。

    token_resolver 的 `is_self_loop_actor` 原语句是无非空过滤的 `SELECT bot_username
    ... LIMIT 1` 且取到值后仍判真值（`if row and row[0]`）再比较——空串/NULL 两种情况
    都落到「不拦截」，与本函数口径等价，故复用不另立具名查询。
    """
    row = read_one_any("repo_bot_username", (repo_key,))
    return str(row[0] or "") if row else ""


def repo_org_id_any(repo_key: str) -> Any:
    """仓库所属组织 id；无行返回 None（调用方按 0/个人组织处理）。"""
    row = read_one_any("repo_org_id", (repo_key,))
    return row[0] if row else None


def set_repo_deploy_url_any(deploy_url: str, repo_key: str) -> int:
    """登记仓库部署地址，返回影响行数；0 行=仓库未托管（不写库）。"""
    return exec_any("repo_deploy_url_set", (deploy_url, repo_key))


# ---------- 存量场景绑定修复（Issue #541；api 一次性迁移） ----------
#
# 原实现把三张本域表的 SQL 写在 api/scene_binding_repair.py 里，用 api 进程主库游标执行。
# 三库拆分后 api 进程库没有 scene_market / devops_pipeline_scene / managed_repos（视图 0），
# 整个扫描被 except 吞掉 → 存量错误绑定永远修不掉（Issue #541 的修复停摆）。
# 这里只暴露「取候选行 / 取解析结果 / 条件改绑」四件事，匹配链规则留在调用方。

def repo_bound_scene_rows_any() -> list[tuple]:
    """所有已绑定默认场景的仓库 (repo_key, dev_scene) 行；空串绑定不计。"""
    return [tuple(r) for r in read_all_any("repo_bound_scene_rows", ())]


def scene_active_official_id_by_name_any(scene_name: str) -> str:
    """official 且启用中的场景 id；同名无启用行返回空串。"""
    row = read_one_any("scene_active_official_id_by_name", (scene_name,))
    return str(row[0] or "") if row else ""


def scene_copy_id_by_market_any(market_scene_id, repo_key: str) -> str:
    """仓库内该 market 模板副本的 scene_id（多副本取 updated_at 最新、同刻取大 id）。"""
    row = read_one_any("scene_copy_by_market_id", (market_scene_id, repo_key))
    return str(row[0] or "") if row else ""


def set_repo_dev_scene_cas_any(repo_key: str, old_scene: str, new_scene: str) -> int:
    """仅当 dev_scene 仍是旧值时才改绑（CAS），返回影响行数；并发已改走返回 0。"""
    return exec_any("repo_dev_scene_set_cas", (new_scene, repo_key, old_scene))


# ---------- 场景市场（scene_market / scene_skill_ref 归 mgmt；runtime 定时同步消费） ----------
#
# runtime 的场景同步原先为每个 mgmt 表各留一份"本地兜底 SQL"（mgmt_lib 无自有 DSN 时
# 返回 None），兜底 SQL 正是跨域表引用的来源。SQL 上移到本域后，调用方只剩业务规则，
# 两条拓扑跑同一份语句：自有 DSN → mgmt 连接；单库/测试 → 进程主库。


def official_scene_id_any(scene_name: str) -> Any | None:
    """按场景名取 official 模板的 id（无则 None）。"""
    row = read_one_any("scene_official_id_by_name", (scene_name,))
    return row[0] if row else None


def official_scene_row_any(scene_name: str) -> Any | None:
    """official 场景的 (id, version, content_hash, is_active)；列序与同步原查询一致。"""
    return read_one_any("scene_official_row_by_name", (scene_name,))


def list_official_scene_ids_names_any() -> list[Any]:
    """全部 official 场景的 (id, name)，供 stale 检测比对仓库清单。"""
    return read_all_any("scene_official_id_names", ())


def count_featured_official_scenes_any() -> int:
    """official 且启用中的精选场景数（精选初始化判断用）。"""
    row = read_one_any("scene_featured_official_count", ())
    return int(row[0] or 0) if row else 0


def mark_default_featured_any(names: list[str]) -> int:
    """把给定名字的 official 启用场景标记为精选（不动的行原样保留），返回影响行数。

    IN 列表长度由调用方给的名字数量决定，占位符在本域拼，表名/列名不来自外部。
    """
    clean = [str(n) for n in (names or []) if str(n or "").strip()]
    if not clean:
        return 0
    placeholders = ",".join("?" * len(clean))
    sql = (f"UPDATE scene_market SET is_featured=1, updated_at=datetime('now') "
           f"WHERE name IN ({placeholders}) AND source='official' AND is_active=1")
    with _any_tx() as c:
        c.execute(sql, tuple(clean))
        return int(getattr(c, "rowcount", 0) or 0)


def replace_scene_skill_ref_any(scene_id: int, refs: list[dict[str, Any]]) -> dict[str, int]:
    """重建某场景的技能引用：同一事务里先删后插（`INSERT OR IGNORE` 幂等）。

    refs: [{"skill_id": int, "skill_name": str}]，列序与 runtime 原写语句一致。
    """
    with _any_tx() as c:
        c.execute("DELETE FROM scene_skill_ref WHERE scene_id=?", (int(scene_id),))
        deleted = int(getattr(c, "rowcount", 0) or 0)
        inserted = 0
        for r in refs or []:
            c.execute(
                "INSERT OR IGNORE INTO scene_skill_ref (scene_id, skill_id, skill_name) "
                "VALUES (?,?,?)",
                (int(scene_id), int(r.get("skill_id") or 0), str(r.get("skill_name") or "")))
            inserted += int(getattr(c, "rowcount", 0) or 0)
    return {"deleted": deleted, "inserted": inserted}


def upsert_scene_content_any(name: str, fields: dict[str, Any], source: str = "official") -> dict[str, Any]:
    """按 content_hash 幂等的场景内容写入（任意拓扑，可移植方言）。

    返回 {"action": inserted|updated|skipped, "id": int|0, "old_version": str|None}。
    只写内容列：**绝不触碰** is_featured / like_count / source_user_id；UPDATE 时
    保留库内既有 is_active（admin 设置不被同步覆盖），INSERT 时按原实现落
    is_featured=0 / like_count=0 / source_user_id=0 / is_active=1。
    """
    payload = {_SCENE_FIELD_ALIASES.get(k, k): v for k, v in (fields or {}).items()}
    content_hash = payload.get("content_hash") or ""
    cols = [c for c in _SCENE_CONTENT_COLS if c in payload]
    with _any_tx() as c:
        c.execute("SELECT id, version, content_hash, is_active, tags, cat_l1_id, cat_l2_id "
                  "FROM scene_market WHERE name=? AND source=? LIMIT 1", (name, source))
        row = c.fetchone()
        if row:
            sid, old_version, old_hash, preserved_active, old_tags = (
                int(row[0]), row[1], (row[2] or ""), row[3], (row[4] or ""))
            old_l1, old_l2 = (row[5] if len(row) > 5 else 0), (row[6] if len(row) > 6 else 0)
            if old_hash == content_hash:
                return {"action": "skipped", "id": sid, "old_version": old_version}
            if not cols:
                return {"action": "skipped", "id": sid, "old_version": old_version}
            if "tags" in payload:
                # Issue #2761：同步写入只补 auto 标签，保留人工(manual)标签
                from src.kernel.tags import encode_tags, merge_tags  # noqa: PLC0415
                payload["tags"] = encode_tags(merge_tags(old_tags, payload["tags"]))
            if "cat_l1_id" in payload:
                # Issue #2761：一级未变且来源未给二级时保留旧二级（未命中不覆盖旧值）
                from src.kernel.category import sync_category_target  # noqa: PLC0415
                payload["cat_l1_id"], payload["cat_l2_id"] = sync_category_target(
                    payload.get("cat_l1_id"), payload.get("cat_l2_id"), old_l1, old_l2)
                if "cat_l2_id" not in cols:
                    cols.append("cat_l2_id")
            sets = ", ".join(f"{col}=?" for col in cols)
            # is_active 原样回写库内既有值（原 SQL 同样是 is_active=? 传 preserved_active，
            # 语义是"不改成 1"，admin 停用状态在同步后保持）
            active = preserved_active if preserved_active is not None else 1
            c.execute(f"UPDATE scene_market SET {sets}, is_active=?, updated_at=datetime('now') "
                      f"WHERE id=?", tuple(payload[col] for col in cols) + (active, sid))
            return {"action": "updated", "id": sid, "old_version": old_version}

        c.execute(
            "INSERT INTO scene_market "
            "(name, display_name, version, description, market_description, icon, obs_icon_key, "
            "category_id, cat_l1_id, cat_l2_id, is_featured, like_count, source, source_user_id, "
            "recommended_template, nodes_json, onboarding, tags, author, "
            "content_hash, preset_custom_fields, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,'official',0,?,?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
            (name, payload.get("display_name", ""), payload.get("version", ""),
             payload.get("description", ""), payload.get("market_description", ""),
             "", "", payload.get("category_id", 0),
             payload.get("cat_l1_id", 0), payload.get("cat_l2_id", 0),
             payload.get("recommended_template", ""), payload.get("nodes_json", ""),
             payload.get("onboarding", ""), payload.get("tags", ""), payload.get("author", ""),
             content_hash, payload.get("preset_custom_fields", "")))
        return {"action": "inserted", "id": int(getattr(c, "lastrowid", 0) or 0),
                "old_version": None}


def set_scene_icon_any(scene_id: int, icon: str, obs_icon_key: str) -> int:
    """回写场景图标（icon 展示地址 + OBS key），按 id 定位，返回影响行数。"""
    return exec_any("scene_set_icon_by_id", (icon or "", obs_icon_key or "", int(scene_id)))


def mapping_user_id_by_column(column: str, value: str) -> Any:
    """按 unified_user_mapping 平台列取映射 user_id（无命中返回 None）。

    列名必须在 `_UM_COLUMN_QUERIES` 白名单内；未登记列抛 KeyError（调用方按原
    实现的 except 口径处理），保证 SQL 不因外部传入的列名变成任意拼接。
    """
    row = read_one_any(_UM_COLUMN_QUERIES[column or ""], (value,))
    return row[0] if row else None


def account_id_by_platform_login(provider: str, login: str) -> Any:
    """平台 login → grape 账号 id（status=1，login 大小写不敏感）；无命中 None。"""
    row = read_one_any("account_id_by_platform_login", (provider, login))
    return row[0] if row else None


def repo_user_id_by_platform_login(provider: str, login: str, repo_key: str,
                                   prefixed_repo_key: str) -> Any:
    """平台 login → 该仓库成员 user_id（三级回退末级）；无命中 None。"""
    row = read_one_any("repo_user_id_by_platform_login",
                       (provider, login, repo_key, prefixed_repo_key))
    return row[0] if row else None


def first_join_user_id_by_repo(repo_key: str, prefixed_repo_key: str) -> Any:
    """取仓库最早加入的成员 user_id（issue_creator 为空时的原逻辑）；无行 None。"""
    row = read_one_any("first_join_user_id_by_repo", (repo_key, prefixed_repo_key))
    return row[0] if row else None


def user_repo_keys_by_username(username: str) -> list[str]:
    """按注册名（大小写不敏感）取该用户已加入的仓库键（is_join=1）。"""
    return [r[0] for r in read_all_any("user_repo_keys_by_username", (username,))]


# ---------- 需求中心（runtime/issues_center）的外域读：仓库镜像 + 平台登录名 ----------
#
# 原实现每处都是「进程库直查 mgmt 表」：单库拓扑正常，三库拓扑下读到的是 runtime 进程库
# 里没有的表（1146）或另一份副本（静默数据分裂）。SQL 只留本域一份，两条拓扑同一语句。


def mapping_user_id_preferring_account(column: str, value: str) -> Any:
    """按 um 平台列取 user_id，优先「登录账号存在且启用」的映射行；无命中 None。

    与 `mapping_user_id_by_column` 的区别是那条 EXISTS 校验（issues_center 的优先链第一跳）；
    列名必须在本域 `_UM_ACCOUNT_COLUMN_QUERIES` 白名单内，未登记列抛 KeyError。
    """
    row = read_one_any(_UM_ACCOUNT_COLUMN_QUERIES[column or ""], (value,))
    return row[0] if row else None


def platform_login_by_user_id_any(user_id: Any) -> str:
    """按 user_id 取平台登录名（gitcode_user 优先、空则 github_login）；无行返回空串。

    列序/COALESCE/NULLIF 与原语句逐字一致；user_id 为空/0 时原 SQL 恒不命中，这里直接空串。
    """
    uid = str(user_id or "")
    if uid in ("", "0"):
        return ""
    row = read_one_any("um_platform_login_by_user", (uid,))
    return str(row[0] or "") if row else ""


def repo_key_exists_any(repo_key: str) -> str:
    """仓库是否在 managed_repos 登记；返回库里的 repo_key 原值，无行返回空串。"""
    if not repo_key:
        return ""
    row = read_one_any("repo_key_row", (repo_key,))
    return str(row[0] or "") if row else ""


def repo_registered_any(repo_key: str) -> bool:
    """仓库是否在 managed_repos 有行（A2A 真伪快判）。

    与 `repo_key_exists_any` 的差别：**空键也照查**——原语句是
    `SELECT 1 ... WHERE repo_key=?`，语义只判「有行」，不能因为键为空就短路
    （调用方据此决定是否走可达性回补录）。
    """
    return read_one_any("repo_registered_flag", (repo_key,)) is not None


def repo_owner_name_any(repo_key: str) -> Any | None:
    """仓库的 (repo_owner, repo_name)；无行返回 None（调用方自判空值再回落）。"""
    return read_one_any("repo_owner_name", (repo_key,))


def ensure_repo_row_any(repo_key: str, repo_owner: str, repo_name: str,
                        full_name: str = "") -> int:
    """任意拓扑幂等补录 managed_repos 最小行；返回 1=新建 / 0=已存在。

    与 `ensure_repo_row` 同口径，但连接自适应（自有 DSN → mgmt；单库 → 进程主库），
    供 runtime 进程在三库拓扑下补录（走独立连接而不是本地兜底 SQL）。
    """
    return exec_any("repo_insert_ignore",
                    (repo_key, repo_owner, repo_name,
                     full_name or f"{repo_owner}/{repo_name}"))


#: 仓库键 IN 查询的分块大小（MySQL/sqlite 占位符上限的保守值）。
_REPO_KEY_QUERY_CHUNK = 400


def repo_keys_existing_any(repo_keys: "list[str]") -> list[str]:
    """给定候选仓库键里已在 managed_repos 登记的那些（每块 `ORDER BY repo_key` 照抄）。

    候选集来自平台卡前缀（实际规模远小于分块上限，单块即与原 `IN (...) ORDER BY repo_key`
    逐字一致）；超过分块上限时按候选顺序逐块查询、块内有序拼接，避免撞参数上限。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    out: list[str] = []
    for start in range(0, len(keys), _REPO_KEY_QUERY_CHUNK):
        chunk = keys[start:start + _REPO_KEY_QUERY_CHUNK]
        ph = ",".join(["?"] * len(chunk))
        with _any_cursor() as c:
            c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({ph}) "
                      f"ORDER BY repo_key", tuple(chunk))
            out.extend(str(r[0]) for r in (c.fetchall() or []) if r and r[0])
    return out


def repo_bot_usernames_any(repo_keys: "list[str]") -> list[str]:
    """给定仓库键的**非空** bot 用户名（去重保序）。

    原调用点是一条 `repo_key IN (...) AND bot_username!=''` 取回后立刻转 set；
    这里按候选顺序分块查询、`dict.fromkeys` 去重——调用方只做成员判断，
    行序不影响语义。空候选集返回 `[]`（原语句会拼出 `IN ()` 语法错并被吞掉）。
    """
    keys = list(dict.fromkeys(str(k) for k in (repo_keys or []) if k))
    if not keys:
        return []
    out: list[str] = []
    for start in range(0, len(keys), _REPO_KEY_QUERY_CHUNK):
        chunk = keys[start:start + _REPO_KEY_QUERY_CHUNK]
        ph = ",".join(["?"] * len(chunk))
        with _any_cursor() as c:
            c.execute(_any_sql("repo_bot_usernames_in").format(ph=ph), tuple(chunk))
            out.extend(str(r[0]) for r in (c.fetchall() or []) if r and r[0])
    return list(dict.fromkeys(out))


def list_active_scene_nodes() -> list[Any]:
    """全部启用场景的 (id, nodes_json)，列序与引擎绑定维护的原查询一致。"""
    return read_all_any("active_scene_nodes", ())


def list_active_scene_bindings() -> list[Any]:
    """全部启用场景的 (id, repo_key, scene_id, nodes_json)（解绑审计用）。"""
    return read_all_any("active_scene_bindings", ())


def list_enabled_autopilot_engine_ids() -> list[Any]:
    """启用中的自动化任务 (id, name, engine_runtime_ids)（引擎解绑提示用）。"""
    return read_all_any("enabled_autopilot_engine_ids", ())


def list_autopilot_engine_ids_nonempty() -> list[Any]:
    """已声明引擎列表的任务 (id, engine_runtime_ids)（引擎 id 重映射用）。"""
    return read_all_any("autopilot_engine_ids_nonempty", ())


def set_autopilot_engine_ids_any(task_id: int, engine_runtime_ids_json: str) -> int:
    """回写任务的引擎列表 JSON（任意拓扑；与 `set_autopilot_engine_ids` 同语义）。"""
    return exec_any("autopilot_engine_ids_set", (engine_runtime_ids_json, int(task_id)))


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


#: 迁移期补列的列名白名单（探测到的列只用于在下面三条已登记语句里挑一条，绝不拼列名）。
_EVENT_ACTION_FILTER_OPTIONAL_COLUMNS: tuple[str, str] = ("block_changes", "vcs_source")


def _event_action_filter_query_name(columns) -> str:
    """按探测到的列集挑已登记语句：与原 api 侧 if/elif/else 三分支同口径。

    原实现的条件是 `has_block and has_vcs` → 全列；`has_block`（vcs 缺）→ 忽略 vcs；
    else（block 缺，无论 vcs 在不在）→ 两者都用常量空串。这里逐条照搬，不「改良」。
    """
    present = {str(name).lower() for name in (columns or ())}
    has_block = _EVENT_ACTION_FILTER_OPTIONAL_COLUMNS[0] in present
    has_vcs = _EVENT_ACTION_FILTER_OPTIONAL_COLUMNS[1] in present
    if has_block and has_vcs:
        return "event_action_filter_enabled"
    if has_block:
        return "event_action_filter_enabled_no_vcs"
    return "event_action_filter_enabled_legacy"


def event_action_filter_enabled_rows() -> list[Any]:
    """启用中的动作过滤器规则（8 列定序，按 priority, id 升序）。

    返回列序 = (event_type, action, allow, actor, require_changes, block_changes,
    handle_side_effect, vcs_source)，与改造前 api 侧三条 SELECT 逐字一致；缺列以空串
    占位（迁移期老库）。**不过滤 status / 任意软删列**：原 SQL 只有 `enabled=1`，
    `enabled=0` 的行照旧不出现、其余行照旧出现。查询失败直接上抛，由调用方降级。
    """
    with _any_cursor() as c:
        c.execute(_ANY_QUERIES["event_action_filter_columns"])
        columns = [d[0] for d in (c.description or [])]
        c.execute(_ANY_QUERIES[_event_action_filter_query_name(columns)])
        return list(c.fetchall() or [])


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


def external_user_id_by_login(provider: str, external_login: str) -> Any:
    """按 (provider, external_login) 取账号 id（external_identities 归 mgmt）；无命中返回 0。

    与 runtime/im/feishu_async 原来那句兜底 SQL 逐列逐字同口径：单列 `ei.grape_user_id`、
    `provider=?` 在前 `external_login=?` 在后、只认 `status=1`（status=4 的失效凭据不算）、
    `LIMIT 1` 取首行。未命中返回 **0**（不是 None）：调用点写的是 `if _uid:`，0 与空串
    等价；而原语句的 `row[0] if row else 0` 也返回 0，保持同一返回值域。

    空 provider / 空 login 直接返回 0 且不发查询（不存在 provider 为空的身份行，
    与同族 `external_user_id` 的入参守卫一致）。
    """
    if not provider or external_login in (None, ""):
        return 0
    row = read_one_any("external_user_id_by_external_login", (provider, str(external_login)))
    return row[0] if row else 0


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


def skill_category_active_map() -> dict[str, int]:
    """**启用中**的技能分类 {slug: id}（`skill_category` 归本域）。

    唯一调用方：`runtime/storage/skill_sync` 的官方技能同步（每 30 分钟一轮）。
    与 `skill_category_map()` 的区别只在过滤口径：保留原语句的 `is_active=1`，
    停用分类不参与新技能的 category_id 归属（原实现的特殊口径，不能顺手并表）。
    列序 `(slug, id)` 与原语句逐字一致；取不到行时返回空 dict。
    """
    with _any_cursor() as c:
        c.execute(_ANY_QUERIES["skill_category_active_ids"])
        return {r[0]: int(r[1]) for r in (c.fetchall() or [])}


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


# ---------- 巡检补偿责任链原语（api/patrol_compensate.py 的六个站点） ----------
# 补偿器原先用进程库 `db` + `db_lock` 直读/直写本域表；三库独立后进程库里没有这张表
# （无跨库视图 → 1146），且 `db_lock` 只能序列化同进程线程，跨进程抢占不再成立。
# 语句上移本域后：自有 DSN → mgmt 连接（语句级原子），单库/测试 → 进程主库 + db_lock。
# 列序与补偿模块原查询逐字一致，调用方按下标解包不用改。

def patrol_finding_row(fid: str):
    """单条 finding 的 (id, kind, task_id, repo_key, issue_id, status)；无行返回 None。"""
    return read_one_any("patrol_finding_row", (fid,))


def patrol_finding_mark(fid: str, status: str, note: str, now_iso: str,
                        limit: int = 2000) -> None:
    """回写 finding 状态，并把 note 追加进 action_meta（读-改-写在同一事务内）。

    仓库规范：禁止在 SQL 里做条件拼接（MySQL 默认 sql_mode 下 `||` 是逻辑 OR，
    会静默把回执写成 '0'/'1'）——先读现值、Python 拼接、普通 UPDATE。合并与截断
    口径与原补偿模块 `db_lock` 段一致：空现值取 note 本身，超过 2000 字符截断。
    """
    with _any_tx() as c:
        c.execute(_any_sql("patrol_finding_meta"), (fid,))
        row = c.fetchone()
        merged = (row[0] or "") if row else ""
        merged = (merged + " | " + note) if merged else note
        c.execute(_any_sql("patrol_finding_set"), (status, merged[:limit], now_iso, fid))


def patrol_finding_claim(fid: str, now_iso: str) -> bool:
    """原子抢占 open→claiming；返回是否抢到（= 原实现 rowcount==1 的判据）。"""
    return exec_any("patrol_finding_claim", (now_iso, fid)) == 1


def patrol_reclaim_stale_claimings(lease_iso: str, note: str = "claiming 租约超时回收",
                                  limit: int = 2000) -> int:
    """把租约超窗（或 updated_at 为空）的 claiming 回收为 open，返回回收条数。

    先扫后写在同一事务内完成（自有 DSN 真事务 / 单库 db_lock），与补偿模块原
    「持锁扫 + 逐行回写 + 末尾一次 commit」等价；重试幂等（已回收的行不再是 claiming）。
    """
    recycled = 0
    with _any_tx() as c:
        c.execute(_any_sql("patrol_findings_stale_claiming"), (lease_iso,))
        for (fid, meta) in list(c.fetchall() or []):
            c.execute(_any_sql("patrol_finding_reopen"),
                      (((meta or "") + " | " + note)[:limit], fid))
            recycled += 1
    return recycled


def patrol_open_finding_ids(cutoff_iso: str, limit: int = 20) -> list:
    """按 created_at FIFO 捞本批待补偿的 finding id（冷却窗外的失败 + 从未尝试的）。

    id 是 UUID，按 id 排序等于随机挑，因此 ORDER BY created_at 是口径的一部分
    （冷却谓词 `action_meta=''` 让首轮 finding 不受冷却限制）。
    """
    return [r[0] for r in read_all_any("patrol_findings_open_ids", (cutoff_iso, limit))]


# ---------- 看板/报表读（insight dashboard 用；表归 mgmt） ----------

def account_name_rows_by_ids(ids, cursor: Any = None) -> list[tuple]:
    """批量取账号行 (id, username)（分块 500/批）。

    只取账号域的用户名（平台无关），不含口令/状态列；`?` 占位符，无排序 ——
    行序与原语句一致（原语句本无 ORDER BY，映射是字典语义）。
    """
    todo = [str(u) for u in (ids or []) if u]
    out: list[tuple] = []
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        ph = ",".join(["?"] * len(part))
        with _owner_cursor(cursor) as c:
            c.execute(f"SELECT id, username FROM grape_accounts WHERE id IN ({ph})", tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def account_names_by_ids(ids) -> dict:
    """批量取账号显示名 {id: username}（分块，500/批）。"""
    out: dict = {}
    for uid, name in account_name_rows_by_ids(ids):
        if name:
            out[uid] = name
    return out


def scene_display_name_rows(scene_ids, cursor: Any = None) -> list[tuple]:
    """批量取场景展示名行 (scene_id, display_name)（分块 500/批）。

    `display_name` 为 NULL **或空串**都回退 `name`；两者都空返回空串 ——
    回退与去重口径（同一 scene_id 首行优先）留在调用方，这里只负责取数。
    NULLIF 两条后端语义一致（不用 MySQL 专有语法）。
    """
    todo = sorted({str(s) for s in (scene_ids or []) if s})
    out: list[tuple] = []
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        ph = ",".join(["?"] * len(part))
        with _owner_cursor(cursor) as c:
            c.execute(
                "SELECT scene_id, COALESCE(NULLIF(display_name,''), NULLIF(name,''))"
                f" FROM devops_pipeline_scene WHERE scene_id IN ({ph})", tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
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


def lease_owner_by_key(lease_key: str, cursor: Any = None) -> str:
    """按 lease_key 取 owner（任意拓扑；空串 = 无行 / owner 为 NULL）。

    只暴露 owner 一列（token 是凭据，消费侧无需要）：原调用方（runtime/im/outbox 的
    `lease_owner_of`）就是这样读的 —— 列序 `COALESCE(lease_owner,'')` 单列逐字照抄。
    `cursor` 非空且单库/测试拓扑 → 复用调用方游标（调用方的库就是本库，不必再开连接，
    也不嵌套 db_lock）；自有 DSN → 忽略它，走 mgmt 连接（那正是原来 1146 的来源）。
    """
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("lease_owner_by_key"), (str(lease_key or ""),))
        row = c.fetchone()
    return str(row[0]) if row and row[0] else ""


def lease_owned_installations_by_owner(owner: str, cursor: Any = None) -> list[tuple]:
    """某 owner 持有的 (installation_id, channel_type)（最多 200 行，最近更新优先）。

    谓词 `lease_owner=? AND installation_id<>''`、`ORDER BY updated_at DESC`、字面
    `LIMIT 200` 全部逐字照抄原语句；`cursor` 语义同 `lease_owner_by_key`。
    """
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("lease_owned_installations_by_owner"), (str(owner or ""),))
        return [tuple(r) for r in (c.fetchall() or [])]


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


def installation_provider_rows_any(conn: Any = None) -> list[tuple]:
    """活跃安装八列 (id, channel_type, scope, owner_user_id, repo_key, config,
    config_fingerprint, status)——Supervisor 的 installation_provider 枚举来源。

    原实现在 runtime 进程里用 `engine_wiring._connect(db_path)`（钉住 runtime 库的 IM
    句柄）直发 SQL：三库拓扑下该库没有本表（视图 0）→ 1146，被 supervisor 的
    `except → log.warning → 下一轮继续` 吞成「一个安装都扫不到」——引擎整体不接管任何
    渠道，日志只留一行 scan round failed，线索指向 DB 抖动而不是「库不对」。

    拓扑取舍（两条拓扑各用各的连接，SQL 只在本域留这一份）：
      - 单库 / 测试：**复用调用方 conn**（sqlite 线程亲和：conn 与其游标都在同一个 worker
        线程内创建/使用；关闭仍归调用方）。`conn=None` → 进程主库 + db_lock。
      - 自有 DSN：**忽略**调用方 conn —— 那是 runtime 库句柄，读不到本域表；改用本域连接。

    `status='active'` 过滤与「不排序」都是原口径：枚举顺序由调用方的哈希环归属决定，
    这里加 ORDER BY 会改变首轮接管顺序，属行为变更而非归位。
    """
    caller_cursor = None
    if conn is not None and not db_registry.uses_own_dsn("mgmt"):
        caller_cursor = conn.cursor()
    with _owner_cursor(caller_cursor) as c:
        c.execute(_any_sql("installation_provider_active_rows"))
        return [tuple(r) for r in (c.fetchall() or [])]


def installation_status_counts_any() -> list[tuple]:
    """按状态分组的安装数 (status, count)（/api/im/status 汇总用）。"""
    return [tuple(r) for r in read_all_any("installation_status_counts")]


def installation_list_any() -> list[tuple]:
    """全部安装八列 (id, channel_type, scope, owner_user_id, repo_key, config, status,
    updated_at)，updated_at 倒序；可见性过滤与 secret 掩码留在调用方。"""
    return [tuple(r) for r in read_all_any("installation_list_all")]


def installation_owner_by_id_any(row_id) -> tuple | None:
    """按 id 取 (scope, owner_user_id, repo_key)（启停前的权限判定）；不存在返回 None。"""
    return read_one_any("installation_owner_by_id", (row_id,))


def installation_detail_by_id_any(row_id) -> tuple | None:
    """按 id 取 (channel_type, scope, owner_user_id, repo_key, config)。"""
    return read_one_any("installation_detail_by_id", (row_id,))


def installation_set_status_any(status: str, row_id) -> int:
    """引擎侧启停：只改 status 与 updated_at（不动 legacy 表），返回影响行数。"""
    return exec_any("installation_update_status", (status, row_id))


def installation_set_config_and_fingerprint_any(cfg_json: str, fingerprint: str, row_id) -> int:
    """渠道 config 与指纹同一语句落库（配置变了指纹必须跟着变），返回影响行数。"""
    return exec_any("installation_update_config_fp", (cfg_json, fingerprint, row_id))


def installation_active_configs_by_type_any(channel_type: str) -> list[tuple]:
    """某渠道类型下全部活跃安装的 (config,)（「是否还有别的安装启用着」判定）。"""
    return [tuple(r) for r in read_all_any("installation_active_configs_by_type",
                                           (channel_type,))]


def installation_repo_app_ids_any(repo_key: str) -> set[str]:
    """某仓库的仓库轨安装（scope='repo'、active）所用 legacy_app_id 集合。

    对应原 `vod_handler_async._repo_app_ids`：A2 收尾后 `devops_agent_session.im_app_id`
    已 DROP，「这个 bot 是不是该仓库的 VOD」只能由本表回答。空值/空串 install 行被
    `COALESCE(...)<>''` 过滤（与原语句一致），结果按原实现转成 str 并去重；无行 → 空集合。
    """
    return {str(r[0]) for r in read_all_any("installation_repo_app_ids", (repo_key,)) if r[0]}


def installation_repo_keys_by_app_any(app_id: str) -> list[str]:
    """某 bot（legacy_app_id）名下已配置 VOD 的仓库轨 repo_key 列表（去重，无序）。

    对应原 `vod_handler_async._list_vod_repos` 的查询部分：只认 scope='repo'、active、
    且 repo_key 非空；**不加 ORDER BY**——原语句是 `SELECT DISTINCT` 无序，调用方按
    到达顺序分页，本轮不擅自固化顺序。返回原值（不做 str 归一），空值已由 SQL 过滤。
    """
    return [r[0] for r in read_all_any("installation_repo_keys_by_app", (app_id,)) if r[0]]


def installation_user_active_rows_any(owner_user_id: Any, cursor: Any = None) -> list[tuple]:
    """某用户名下 active 安装的三列 (id, channel_type, config)，updated_at 倒序。

    对应原 `notify.active_targets` 的查询：只认 scope='user' 且 status='active'（revoked /
    disabled 都不参与扇出——这是通知口径，不是回显口径，别与
    `installation_user_channel_rows_any` 混用）。**不做去重**：同渠道多条并存时原实现
    按首见保留（`seen` 集合在调用方），这里原样给出全部行。

    `cursor` 语义同 `account_id_by_username_ci_any`：单库 / 测试拓扑复用调用方游标
    （调用方的库就是本域库），自有 DSN 拓扑忽略它、走 mgmt 连接。调用方（im/notify）
    的连接是**调用期**按 `DEVOPS_DB_PATH` 现取的，而 `*_any` 回落的是 import 期绑定的
    进程主库 —— 不带游标，测试里那种「每用例换库」的 fixture 会读到另一个库。
    """
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("installation_user_active_rows"), (str(owner_user_id),))
        return [tuple(r) for r in (c.fetchall() or [])]


def installation_p2p_chat_ids_any(installation_id: Any, cursor: Any = None) -> list[tuple]:
    """该安装下的 p2p 会话 id 单列 `(channel_chat_id,)`，updated_at 倒序。

    对应原 `notify.p2p_chat_id` 的查询：chat_type='p2p' 且 channel_chat_id 非空；
    **不加 LIMIT**（原语句没有），「优先非话题会话（不含 '#'）」的挑选规则留在调用方
    —— 那是渠道方言（Slack `Dxxx#ts`）的业务口径，不属于本域。`cursor` 语义同上。
    """
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("chat_binding_p2p_channels"), (str(installation_id),))
        return [tuple(r) for r in (c.fetchall() or [])]


def outbound_card_put_any(out_track_id: str, installation_id: str = "",
                          channel_type: str = "dingtalk", channel_chat_id: str = "",
                          chat_type: str = "p2p", sender_staff_id: str = "",
                          card_template_id: str = "",
                          cursor: Any = None) -> int:
    """互动卡片台账整行登记（主键 out_track_id），返回影响行数。

    对应原 `im/engine/outbound_cards._save_sync` 的 `INSERT OR REPLACE`：**整行替换**，
    不是 outbox 那条「只更新状态」的部分 upsert（两者列集与语义都不同，别互相复用）。
    默认值（`channel_type='dingtalk'` / `chat_type='p2p'`）与原语句逐字一致：
    原实现把 `entry.get(...) or "<默认>"` 算好后当参数传，这里由调用点保持同一口径，
    函数只做参数转发、不替调用方填默认值——传入什么就写什么。

    `cursor` 语义见 `_owner_write_cursor`：单库/测试拓扑复用调用方游标（提交由调用方
    负责，调用点保留原 `conn.commit()`）；自有 DSN 拓扑忽略它、走 mgmt 连接（autocommit）。
    """
    with _owner_write_cursor(cursor) as c:
        cur = c.execute(_any_sql("outbound_card_put"),
                        (out_track_id, installation_id, channel_type, channel_chat_id,
                         chat_type, sender_staff_id, card_template_id))
        return int(getattr(cur, "rowcount", 0) or 0)


def outbound_card_chat_row_any(out_track_id: str, cursor: Any = None) -> Any | None:
    """按 outTrackId 取卡片台账的 5 列会话信息，无行返回 None。

    对应原 `im/engine/outbound_cards._load_sync` 的查询：列序
    `(channel_chat_id, chat_type, sender_staff_id, installation_id, card_template_id)`
    逐字照抄；**返回整行**（不是拆成 dict）——「行在但某列为 NULL」与「无行」在原实现里
    是两个分支（前者用 `or` 兜默认值、后者返回 None），返回标量会合并它们。
    `cursor` 语义同 `installation_p2p_chat_ids_any`。
    """
    with _owner_cursor(cursor) as c:
        c.execute(_any_sql("outbound_card_chat_row"), (out_track_id,))
        return c.fetchone()


# ---------- GSB 服务注册表（kernel/gsb/registry.py 的 SQL 全部收在此处） ----------
#
# 归位理由：gsb_service_registry 是唯一一张「所有域进程都写」的基础设施表，归属 mgmt
# （configs/table_ownership.json）。原实现在 kernel 里用进程主库直连——单库拓扑没问
# 题，三库拓扑下每个域把注册写进自己那个库：注册不报错，但跨域互相发现不到（静默失效，
# 不是能看到 1146 的那种故障）。SQL 收到本域后，任一域进程的注册/心跳/发现都落同一张
# mgmt 表；单库 / 测试拓扑由任意拓扑原语回落进程主库，行为与改前逐字一致。
# 方言：`?` 占位符 + INSERT OR REPLACE（mysql_compat 翻译成 REPLACE INTO）。


def gsb_registry_ddl() -> str:
    """注册表建表语句（DDL 走函数返回：调用方不得 import 常量属性）。"""
    return (
        "CREATE TABLE IF NOT EXISTS gsb_service_registry (\n"
        "    service_name   TEXT PRIMARY KEY,\n"
        "    base_url       TEXT NOT NULL,\n"
        "    health_url     TEXT,\n"
        "    version        TEXT DEFAULT '1',\n"
        "    status         TEXT DEFAULT 'online',\n"
        "    last_heartbeat REAL DEFAULT 0,\n"
        "    metadata       TEXT DEFAULT '{}'\n"
        ")"
    )


def ensure_gsb_registry_schema() -> None:
    """幂等建表 + 旧库补 version 列。

    ``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列，而注册语句显式写 version：
    旧库上注册会被异常吞成无限重试，所以补一次 best-effort 的 ADD COLUMN。列已存在时
    忽略报错（SQLite / MySQL 文案不同），只落 debug 日志。
    """
    with _any_cursor(write=True) as c:
        c.execute(gsb_registry_ddl())
        try:
            c.execute("ALTER TABLE gsb_service_registry "
                      "ADD COLUMN version TEXT DEFAULT '1'")
        except Exception as exc:  # noqa: BLE001 - 列已存在（两种后端报错文案不同）
            log.debug(f"gsb registry version column already present: {exc}")


def _any_dict_rows(name: str, params: tuple = ()) -> list[dict[str, Any]]:
    """按具名查询读多行并转 dict。

    列名取自游标 description（SQLite 行工厂 / MySQL DictCursor 均可），与 kernel 原
    `_rows_to_dicts` 同口径；空结果返回空列表（不返回 None，调用方无需判空）。
    """
    with _any_cursor() as c:
        c.execute(_any_sql(name), params)
        rows = list(c.fetchall() or [])
        cols = [d[0] for d in c.description] if c.description else []
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    return [dict(zip(cols, r)) for r in rows]


def gsb_register(name: str, base_url: str, health_url: Any, version: str,
                 last_heartbeat: float, metadata_json: str) -> int:
    """注册/覆盖一条服务实例行（主键 service_name 覆盖，语义同 INSERT OR REPLACE）。

    status 固定写回 'online'：与原语句一致，重新注册即视为上线。
    """
    return exec_any("gsb_service_upsert",
                    (name, base_url, health_url, version, last_heartbeat, metadata_json))


def gsb_heartbeat(name: str, last_heartbeat: float) -> int:
    """刷新某服务的心跳时间并置 online，返回影响行数（0=该行已被删）。"""
    return exec_any("gsb_service_heartbeat", (last_heartbeat, name))


def gsb_discover_row(name: str) -> dict[str, Any]:
    """按服务名取 online 行（全列）；无行返回 {}（心跳超时判定留在调用方 GSB 语义里）。"""
    rows = _any_dict_rows("gsb_service_online_row", (name,))
    return rows[0] if rows else {}


def gsb_online_services(cutoff: float) -> list[dict[str, Any]]:
    """心跳晚于 cutoff 的全部 online 行，按服务名排序。"""
    return _any_dict_rows("gsb_services_online", (cutoff,))


def gsb_instances(service: str, cutoff: float, include_offline: bool = False) -> list[dict[str, Any]]:
    """某基础服务的全部实例行（裸键 ``service`` 与 ``service@ns`` 两类）。

    通配符值 ``service@%`` 在本域拼（是参数值，不是表名/列名，调用方不得传 SQL 片段）；
    不取名 ``include_offline`` 时只保留 online 且心跳晚于 cutoff 的行。
    """
    pattern = f"{service}@%"
    if include_offline:
        return _any_dict_rows("gsb_instances", (service, pattern))
    return _any_dict_rows("gsb_instances_online", (service, pattern, cutoff))


def gsb_all_services(include_offline: bool = True) -> list[dict[str, Any]]:
    """注册表全部行（运维视角）；include_offline=False 只看 status='online'。"""
    name = "gsb_services_all" if include_offline else "gsb_services_online_only"
    return _any_dict_rows(name)


def gsb_mark_offline(name: str) -> int:
    """注销：把该服务行标 offline（保留行与 base_url/metadata 便于排查）。"""
    return exec_any("gsb_service_mark_offline", (name,))


def gsb_purge_stale(cutoff: float) -> int:
    """把心跳早于 cutoff 的 online 行标 offline，返回受影响行数。"""
    return exec_any("gsb_service_purge_stale", (cutoff,))


# ---------- 反馈状态联动（api/feedback_status.py；任意拓扑） ----------
# user_feedback 归本域。原实现从 api 进程主库直发四条 SQL（两条读、两条写），
# 三库拆分后 api 进程库没有该表（视图 0）→ 两处顶层 except 把 1146 吞成
# 「0 条更新」：反馈状态联动（#673）在独立部署下永久无效且日志看不出。
# 这里只交出数据与回写动作，状态推导/只升不降/游标轮转仍留在调用方（那是业务口径）。


def feedback_external_sync_page(after_id: Any, limit: int) -> list[tuple]:
    """外部 Issue 联动的待同步行 [(id, status, repo_key, issue_number, issue_url)]。

    列序与原查询逐字一致（调用方按下标解包）。只取 source='user'、已转换
    （issue_url 非空）、无内部 issue 映射（issue_id 为 NULL 或 0）、未软删、
    且状态不为 closed 的行；按 id 升序键集分页（游标语义在调用方）。
    """
    if limit is None or int(limit) <= 0:
        return []
    return [tuple(r) for r in read_all_any(
        "feedback_external_sync_rows", (after_id, int(limit)))]


def feedback_mark_status_if_live(fid: Any, status: str) -> int:
    """回写单行状态（`is_deleted=0` 守卫，与原外部联动 UPDATE 逐字一致）。

    原实现对每行独立提交，这里保持自提交（exec_any），不并入批量事务。
    """
    return exec_any("feedback_external_mark_status", (status, fid))


def feedback_internal_sync_page(after_id: Any, limit: int) -> list[tuple]:
    """内部映射联动的待评估行 [(id, status, issue_id)]，按 id 升序键集分页。

    列序与原 JOIN 语句的左表列逐字一致；右表（issue，runtime 域）由调用方取回后
    在 Python 侧做内连接。谓词照抄：issue_url 非空、issue_id 非 NULL、未软删。
    """
    if limit is None or int(limit) <= 0:
        return []
    return [tuple(r) for r in read_all_any(
        "feedback_internal_sync_page", (after_id, int(limit)))]


def feedback_apply_statuses(pairs: list[tuple]) -> int:
    """批量回写 [(status, id)]，单事务（`_any_tx`），返回执行条数。

    原内部联动在同一个 `db_lock` 里逐行 UPDATE、末尾统一 commit；这里用跨域
    多语句事务原语保持同一原子口径（异常整体回滚，不留半批）。语句无
    is_deleted 守卫（原语句也没有），逐字照抄。
    """
    todo = [(status, fid) for status, fid in (pairs or [])]
    if not todo:
        return 0
    done = 0
    with _any_tx() as c:
        for status, fid in todo:
            c.execute(_any_sql("feedback_internal_set_status"), (status, fid))
            done += 1
    return done


# ---------- 反馈 → Issue 转换（api/feedback_issue_converter.py；任意拓扑） ----------
# user_feedback 归本域。原实现从 api 进程主库直发「读待转换队列 + 组内逐行回写」，
# 三库拆分后 api 进程库没有该表（视图 0）→ `_fetch_pending` 抛 1146 被
# `feedback_reconcile_loop` 的 except 吞掉：转换器每轮空转，反馈永远变不成 Issue，
# 且日志只有一条 reconcile failed，看不出根因。
# 这里只交出「取哪些行」与「回写哪几列」，幂等分组 / 路由 / 正文组装仍留在调用方。


def feedback_pending_conversions(limit: int) -> list[tuple]:
    """待转换反馈行，15 列元组，列序与原 `_fetch_pending` 逐字一致。

    列序 `(id, repo_key, task_id, agent_type, skill_name, skill_scope, title,
    description, fail_summary, event_seq, issue_id, issue_number, feedback_kind,
    source, username)`（调用方按下标解包）。

    入选口径逐字照抄原语句：`issue_url=''`（未转换）、`is_deleted=0`，且
    `source='agent'` 或 `source='user'` 且 `COALESCE(repo_key,'')!=''`（Issue #673：
    纯意见类用户反馈无仓库上下文，跳过不转换）。**不按 status / username 过滤**，
    也不加 agent_type 过滤。`limit` 直接进 SQL 的 `LIMIT ?`（不在这里截断；
    `0` 取空、负值与 SQLite 口径一致 = 不设上限，保持与原语句同义）。
    """
    return [tuple(r) for r in read_all_any("feedback_pending_rows", (int(limit),))]


def feedback_mark_converted(items: list[tuple]) -> int:
    """批量回写 `[(issue_url, issue_number, converted_at, id)]`，单事务，返回影响行数。

    原实现是「同一把 db_lock 里逐行 UPDATE、末尾一次 commit」——组内多行要么全写、
    要么全不写（同组共用同一个 Issue，半批会让同组行分属不同 Issue）。这里用
    `_any_tx()` 保持该原子口径：自有 DSN 走独立非 autocommit 连接，单库 / 测试
    拓扑走进程主库 + db_lock。

    `WHERE id=? AND issue_url=''` 守卫逐字保留：并发 / 重放时已转换的行不会被覆盖
    （影响行数为 0，调用方据此感知，但原实现的统计口径是「按组 +1」，交回调用方）。
    """
    todo = [tuple(it) for it in (items or [])]
    if not todo:
        return 0
    done = 0
    with _any_tx() as c:
        for url, number, converted_at, fid in todo:
            c.execute(_any_sql("feedback_mark_converted"), (url, number, converted_at, fid))
            # rowcount 读游标（pymysql 的 execute() 返回的是 int，取不到 .rowcount）
            done += int(getattr(c, "rowcount", 0) or 0)
    return done


def feedback_claim_row(claim_value: str, fid) -> bool:
    """把一行待转换反馈「占位」给当前 worker；抢到返回 True。

    单条条件 UPDATE（`WHERE id=? AND COALESCE(issue_url,'')=''`）+ rowcount 判定，
    在共享库的多进程 / 多机下是原子的：只有第一个 worker 能拿到这一行。
    各机各自独立库时该占位只在本库可见，此时防重复靠调用方的「同标题开放单探测」。
    """
    with _any_tx() as c:
        c.execute(_any_sql("feedback_claim_row"), (claim_value, fid))
        return int(getattr(c, "rowcount", 0) or 0) == 1


def feedback_release_claim(fid, prefix: str) -> int:
    """清掉本 worker 的占位（建单失败 / 过期回收），返回影响行数。"""
    with _any_tx() as c:
        c.execute(_any_sql("feedback_release_claim"), (fid, f"{prefix}%"))
        return int(getattr(c, "rowcount", 0) or 0)


def feedback_list_claims(prefix: str) -> list[tuple]:
    """当前占位行 `(id, issue_url)`：供调用方按占位里的时间戳判定是否过期。"""
    return [tuple(r) for r in read_all_any("feedback_list_claims", (f"{prefix}%",))]


def feedback_mark_converted_from_claim(items: list[tuple], prefix: str) -> int:
    """同 `feedback_mark_converted`，但守卫放宽为「空 或 本次占位」。

    `items` 为 `[(issue_url, issue_number, converted_at, id)]`。占位状态下回写若只认
    `issue_url=''`，单已建出来却永远记不上（占位挡着自己写），故单列一条语句。
    """
    todo = [tuple(it) for it in (items or [])]
    if not todo:
        return 0
    done = 0
    with _any_tx() as c:
        for url, number, converted_at, fid in todo:
            c.execute(_any_sql("feedback_mark_converted_from_claim"),
                      (url, number, converted_at, fid, f"{prefix}%"))
            done += int(getattr(c, "rowcount", 0) or 0)
    return done
