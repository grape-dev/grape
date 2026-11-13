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
    """本域 lib 是否可用：**必须配置了本域自有 DSN**（`uses_own_dsn`）。

    收紧原因（架构审查 F2）：lib 写原语使用 MySQL 方言（`%s` / `ON DUPLICATE KEY` /
    `NOW()`）。若允许"复用进程库"的路径也返回可用，在 sqlite 后端（单实例/测试拓扑）
    开启 lib 开关会导致绑定参数错误。要求自有 DSN 后，lib 只在三库三机拓扑下生效，
    复用进程库时一律走调用方原有 SQL 兜底。
    """
    from src.kernel import db_registry
    return db_registry.uses_own_dsn("runtime")


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


_RUNTIME_COLS = (
    "id", "name", "display_name", "installed", "cli_path", "version", "healthy",
    "pip_package", "updated_at", "scope", "owner_id", "credential_id", "status",
    "last_seen_at", "local_skills", "skill_scan_result", "agent_tags", "host_name",
    "max_concurrent", "scene_tags", "alias", "worker_version", "org_id",
    "spawn_fail_degraded_at", "detached_from_scenes_at", "probe_spawn_fail_count",
)


def list_runtimes_rows(scope: str | None = "global", owner_id: str = "") -> list[tuple]:
    """与 mgmt 侧原 SELECT 完全同列序的行集（供灰度切换时零差异比对）。"""
    cols = ", ".join(_RUNTIME_COLS)
    if scope is None:
        sql = f"SELECT {cols} FROM agent_runtimes ORDER BY name"
        args: tuple = ()
    else:
        sql = (f"SELECT {cols} FROM agent_runtimes "
               f"WHERE scope=%s AND (owner_id=%s OR owner_id='') ORDER BY name")
        args = (scope, owner_id)
    with db_registry.cursor("runtime") as c:
        c.execute(sql, args)
        return [tuple(r) for r in c.fetchall()]


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


_CRED_COLS = ("id", "name", "device_name", "scope", "owner_id", "owner_name",
              "status", "last_seen_at", "last_ip", "repo_key", "org_id", "spec")


def list_credential_rows() -> list[tuple]:
    """runtime_credentials 全量行（列序与 mgmt._build_cred_map 原查询一致）。

    只返回**密文/元数据列**（spec 为密文结构），解密仍由调用方负责，
    lib 不接触明文凭据。
    """
    cols = ", ".join(_CRED_COLS)
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT {cols} FROM runtime_credentials")
        return [tuple(r) for r in c.fetchall()]


def latest_task_for_event(event_seq: int) -> tuple | None:
    """事件对应的最近一条任务（id, status, runtime_id, exit_code, started_at,
    completed_at, result_text, skill）；无则 None。与 mgmt feedback 原查询同列序。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, status, runtime_id, exit_code, started_at, completed_at, "
                  "result_text, skill FROM agent_tasks WHERE event_seq=%s "
                  "ORDER BY created_at DESC LIMIT 1", (event_seq,))
        row = c.fetchone()
        return tuple(row) if row else None


def task_brief(task_id: int) -> tuple | None:
    """任务摘要（event_seq, agent_type, skill, runtime_id, repo_key）；无则 None。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT event_seq, agent_type, skill, runtime_id, repo_key "
                  "FROM agent_tasks WHERE id=%s LIMIT 1", (task_id,))
        row = c.fetchone()
        return tuple(row) if row else None


_SKILL_DETAIL_COLS = ("id", "skill_name", "display_name", "description", "agent_type",
                      "skill_path", "file_path", "scope", "owner_id", "content_hash")


def get_skill_by_id(skill_id: int) -> dict[str, Any] | None:
    """按主键取启用中的 skill（与 mgmt.user.db.get_skill_by_id 同字段同语义）。"""
    if not skill_id:
        return None
    cols = ", ".join(_SKILL_DETAIL_COLS)
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT {cols} FROM skill WHERE id=%s AND is_active=1", (int(skill_id),))
        row = c.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "skill_name": row[1], "display_name": row[2] or row[1],
            "description": row[3] or "", "agent_type": row[4] or "",
            "skill_path": row[5] or row[1], "file_path": row[6] or "",
            "scope": row[7] or "official", "owner_id": row[8] or "",
            "content_hash": row[9] or "",
        }


def get_skill_content_hash(skill_name: str, scope: str = "official") -> tuple[int, str, str]:
    """返回 (id, content_hash, file_path)；id=0 表示不存在（与 mgmt 侧同语义）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, content_hash, file_path FROM skill "
                  "WHERE skill_name=%s AND scope=%s LIMIT 1", (skill_name, scope))
        row = c.fetchone()
        if not row:
            return (0, "", "")
        return (row[0], row[1] or "", row[2] or "")


def count_skills(active_only: bool = True) -> int:
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT COUNT(*) FROM skill" + (" WHERE is_active=1" if active_only else ""))
        return int(c.fetchone()[0])


def aggregate_counts() -> dict[str, dict[str, int]]:
    """events / agent_tasks / agent_runtimes 按 status 的计数（供 insight 看板）。"""
    out: dict[str, dict[str, int]] = {}
    with db_registry.cursor("runtime") as c:
        for key, table in (("events_by_status", "events"),
                           ("tasks_by_status", "agent_tasks"),
                           ("runtimes_by_status", "agent_runtimes")):
            try:
                c.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status")
                out[key] = {(r[0] or ""): r[1] for r in c.fetchall()}
            except Exception:  # noqa: BLE001
                out[key] = {}
    return out


_REPO_CRED_COLS = ("id", "repo_key", "cred_type", "value", "updated_at")


def list_repo_credentials(repo_key: str = "") -> list[tuple]:
    """仓库凭据行（只返回密文 value 列，解密归调用方）。"""
    cols = ", ".join(_REPO_CRED_COLS)
    with db_registry.cursor("runtime") as c:
        if repo_key:
            c.execute(f"SELECT {cols} FROM grape_t_repo_credentials WHERE repo_key=%s", (repo_key,))
        else:
            c.execute(f"SELECT {cols} FROM grape_t_repo_credentials")
        return [tuple(r) for r in c.fetchall()]


_EC_COLS = ("id", "runtime_id", "tag", "max_concurrent", "priority", "enabled",
            "created_at", "updated_at")
_ASK_COLS = ("id", "scope", "repo_key", "agent_type", "issue_num", "file_path", "status",
             "source_seq", "created_by", "created_at", "obs_key", "content_hash", "updated_at",
             "source", "archived", "description", "name", "org_id")


def has_engine_capability(runtime_id: int) -> bool:
    """该引擎是否有能力记录（mgmt 侧用 SELECT 1 判定）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=%s LIMIT 1", (int(runtime_id),))
        return c.fetchone() is not None


def list_engine_capabilities(runtime_id: int, enabled_only: bool = False) -> list[tuple]:
    """引擎能力行（id, tag, priority, enabled），按 tag 排序。"""
    sql = "SELECT id, tag, priority, enabled FROM engine_capabilities WHERE runtime_id=%s"
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY tag"
    with db_registry.cursor("runtime") as c:
        c.execute(sql, (int(runtime_id),))
        return [tuple(r) for r in c.fetchall()]


def list_engine_tags(runtime_id: int, enabled_only: bool = True) -> list[str]:
    """引擎能力标签列表（mgmt 展示/校验用）。"""
    return [r[1] for r in list_engine_capabilities(runtime_id, enabled_only)]


_EVENT_COLS = {
    "seq", "repo_key", "issue_id", "event_type", "agent_type", "title", "summary", "actor",
    "status", "created_at", "started_at", "completed_at", "runtime", "skill", "exit_code",
    "duration", "entity_type", "org_id", "source", "result", "scene_id", "payload",
    "agent_logs", "structured_output", "idempotency_key",
}


def _check_select(select: str) -> str:
    """允许 `列, 列` 或 `COUNT(*)` / `SUM(列)` / `列, COUNT(*)` 形式，其余拒绝。"""
    parts = [p.strip() for p in select.split(",") if p.strip()]
    if not parts:
        raise ValueError("select 不能为空")
    for part in parts:
        upper = part.upper()
        if upper == "COUNT(*)":
            continue
        if upper.startswith("SUM(") and upper.endswith(")"):
            col = part[4:-1].strip()
            if col not in _EVENT_COLS:
                raise ValueError(f"SUM 列 '{col}' 不在 events 允许列表内")
            continue
        if " AS " in upper:
            col = part.split(" ", 1)[0].strip()
            if col not in _EVENT_COLS:
                raise ValueError(f"列 '{col}' 不在 events 允许列表内")
            continue
        if part not in _EVENT_COLS:
            raise ValueError(f"列 '{part}' 不在 events 允许列表内")
    return select


def _check_where(where: str) -> None:
    low = where.lower()
    for bad in (";", "--", "/*", "*/"):
        if bad in where:
            raise ValueError("where 片段不得包含分号或注释")
    if " from " in low or " join " in low:
        raise ValueError("where 片段不得包含子查询表引用")


def query_events(select: str, where: str = "", params: tuple = (), order_by: str = "",
                 limit: int = 0, offset: int = 0) -> list[tuple]:
    """events 受控查询：列/表达式白名单 + where 片段校验。

    **占位符必须用 `?`**：lib 连接走 mysql_compat 的方言翻译，`%s` 会被当作字面百分号
    转义掉（实测报 `not all arguments converted during string formatting`）。
    """
    _check_select(select)
    if where:
        _check_where(where)
    sql = f"SELECT {select} FROM events"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
    with db_registry.cursor("runtime") as c:
        c.execute(sql, params)
        return [tuple(r) for r in c.fetchall()]


def group_events(by: str, where: str = "", params: tuple = (),
                 order_by: str = "", limit: int = 0) -> list[tuple]:
    """按列分组计数：返回 [(组值..., 计数)]。

    `by` 只允许白名单列（可用逗号分隔多列），不接受表达式。
    """
    cols = [c.strip() for c in by.split(",") if c.strip()]
    if not cols:
        raise ValueError("by 不能为空")
    for col in cols:
        if col not in _EVENT_COLS:
            raise ValueError(f"分组列 '{col}' 不在 events 允许列表内")
    if where:
        _check_where(where)
    sql = f"SELECT {', '.join(cols)}, COUNT(*) FROM events"
    if where:
        sql += f" WHERE {where}"
    sql += f" GROUP BY {', '.join(cols)}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db_registry.cursor("runtime") as c:
        c.execute(sql, params)
        return [tuple(r) for r in c.fetchall()]


def count_events(where: str = "", params: tuple = ()) -> int:
    rows = query_events("COUNT(*)", where, params)
    return int(rows[0][0]) if rows else 0


def get_autopilot_execution(exec_id: int) -> tuple | None:
    """取自动化执行行 (task_id, findings_json, result, status)；不存在返回 None。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT task_id, findings_json, result, status "
                  "FROM autopilot_executions WHERE id=%s", (int(exec_id),))
        row = c.fetchone()
        return tuple(row) if row else None


def upsert_engine_capability(runtime_id: int, tag: str, priority: int = 50,
                             max_concurrent: int = 1, enabled: int = 1) -> None:
    """写入/更新引擎能力（幂等 UPSERT，供跨域写补偿重放）。"""
    with db_registry.cursor("runtime") as c:
        c.execute(
            "INSERT INTO engine_capabilities "
            "(runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE priority=VALUES(priority), "
            "max_concurrent=VALUES(max_concurrent), enabled=VALUES(enabled), updated_at=NOW()",
            (int(runtime_id), tag, int(max_concurrent), int(priority), int(enabled)),
        )


def update_engine_capability(runtime_id: int, tag: str, priority: int, enabled: int) -> int:
    """更新引擎能力优先级/启用态，返回受影响行数（0 = 不存在，供 404 判定）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE engine_capabilities SET priority=%s, enabled=%s, updated_at=NOW() "
                  "WHERE runtime_id=%s AND tag=%s",
                  (int(priority), int(enabled), int(runtime_id), tag))
        return int(c.rowcount or 0)


def delete_engine_capability(runtime_id: int, tag: str | None = None) -> int:
    """删除引擎能力（tag 为空则删该引擎全部），返回受影响行数；天然幂等。"""
    with db_registry.cursor("runtime") as c:
        if tag:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=%s AND tag=%s",
                      (int(runtime_id), tag))
        else:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=%s", (int(runtime_id),))
        return int(c.rowcount or 0)


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
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
                  "lease_expires_at=%s, lease_acquired_at=?, updated_at=%s "
                  "WHERE lease_key=%s AND (lease_token IS NULL OR lease_token=%s "
                  "OR lease_expires_at IS NULL OR lease_expires_at < %s)"
                  .replace("lease_acquired_at=?", "lease_acquired_at=IFNULL(lease_acquired_at, %s)"),
                  (token, owner, expires, stamp, stamp, lease_key, token, stamp))
        if int(c.rowcount or 0) > 0:
            return True
        try:
            c.execute("INSERT INTO grape_t_channel_lease "
                      "(lease_key, channel_type, lease_token, lease_owner, lease_expires_at, "
                      "lease_acquired_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                      (lease_key, lease_key.split(":")[0] if ":" in lease_key else "", token,
                       owner, expires, stamp, stamp))
            return True
        except Exception:  # noqa: BLE001 - 并发抢占时主键冲突即为失败
            return False


def release_channel_lease(lease_key: str, token: str) -> bool:
    """释放租约：仅当 token 匹配时清空（幂等：已释放则返回 False）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE grape_t_channel_lease SET lease_token=NULL, lease_owner=NULL, "
                  "lease_expires_at=NULL, updated_at=NOW() "
                  "WHERE lease_key=%s AND lease_token=%s", (lease_key, token))
        return int(c.rowcount or 0) > 0


def heartbeat_instance(instance_id: str, hostname: str = "", ip: str = "",
                       im_enabled: int = 1) -> None:
    """实例心跳 UPSERT（`grape_im_instance` 为 runtime 基表）。

    时间列统一存 UTC 字符串 "%Y-%m-%d %H:%M:%S"（与 outbox / bot_lease 口径一致），
    用原生 MySQL `ON DUPLICATE KEY UPDATE`，不依赖方言翻译层。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT INTO grape_im_instance "
                  "(instance_id, hostname, ip, joined_at, last_heartbeat, im_enabled) "
                  "VALUES (%s, %s, %s, %s, %s, %s) "
                  "ON DUPLICATE KEY UPDATE hostname=VALUES(hostname), ip=VALUES(ip), "
                  "last_heartbeat=VALUES(last_heartbeat), im_enabled=VALUES(im_enabled)",
                  (instance_id, hostname, ip, now, now, int(im_enabled)))


def list_active_instances(timeout_s: float = 45.0) -> list[str]:
    """列出 timeout 秒内仍有心跳的 instance_id（用于判断谁在活跃持有租约）。"""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=float(timeout_s))).strftime("%Y-%m-%d %H:%M:%S")
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT instance_id FROM grape_im_instance "
                  "WHERE last_heartbeat >= %s AND im_enabled=1 ORDER BY instance_id", (cutoff,))
        return [r[0] for r in c.fetchall()]


def release_all_leases(pairs: list[tuple[str, str]]) -> int:
    """批量释放：pairs 为 [(lease_key, token)]，返回实际释放条数。"""
    released = 0
    for lease_key, token in pairs or []:
        if release_channel_lease(lease_key, token):
            released += 1
    return released


def renew_channel_lease(lease_key: str, token: str, owner: str = "", ttl_seconds: int = 60,
                        expected_token: str | None = None) -> bool:
    """IM 渠道租约续约（CAS 语义，可重放）。

    仅当 `lease_token` 为空或等于 `expected_token` 时才写入，避免抢掉别人的租约。
    返回是否成功写入。
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
    stamp = now.isoformat()
    if expected_token:
        sql = ("UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
               "lease_expires_at=%s, lease_acquired_at=IFNULL(lease_acquired_at, %s), updated_at=%s "
               "WHERE lease_key=%s AND (lease_token IS NULL OR lease_token=%s)")
        args = (token, owner, expires, stamp, stamp, lease_key, expected_token)
    else:
        sql = ("UPDATE grape_t_channel_lease SET lease_token=%s, lease_owner=%s, "
               "lease_expires_at=%s, lease_acquired_at=IFNULL(lease_acquired_at, %s), updated_at=%s "
               "WHERE lease_key=%s AND lease_token IS NULL")
        args = (token, owner, expires, stamp, stamp, lease_key)
    with db_registry.cursor("runtime") as c:
        c.execute(sql, args)
        return int(c.rowcount or 0) > 0


# ---------- 会话/任务写原语（P3-5g：mgmt / insight 进程写 runtime 基表） ----------

def purge_user_rows(user_id: str) -> dict[str, int]:
    """账号删除级联：按 user_id 一并清理 runtime 域业务数据。

    覆盖 agent_tasks / chat_agent_session / chat_conversations / issue 四表。原实现是
    mgmt 侧在同一事务里逐表 DELETE —— 拆成四条意图会丢"一起删"的语义，故做成一个原语。
    """
    uid = str(user_id)
    out: dict[str, int] = {}
    with db_registry.cursor("runtime") as c:
        for table in ("agent_tasks", "chat_agent_session", "chat_conversations", "issue"):
            c.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))
            out[table] = int(c.rowcount or 0)
    return out


def delete_chat_conversations(im_channel_id: str = "") -> int:
    """按 IM 渠道清空 chat 会话（换绑 / 解绑清理）。"""
    if not im_channel_id:
        return 0
    with db_registry.cursor("runtime") as c:
        c.execute("DELETE FROM chat_conversations WHERE im_channel_id=%s", (im_channel_id,))
        return int(c.rowcount or 0)


def delete_vod_conversations(agent_session_ids: list[str] | None = None,
                             repo_key: str = "") -> int:
    """删 VOD 会话：按 agent_session_id 列表，或按某 repo 的 vod 会话（二者择一）。"""
    with db_registry.cursor("runtime") as c:
        if agent_session_ids:
            ids = [str(i) for i in agent_session_ids if str(i)]
            if not ids:
                return 0
            ph = ",".join(["%s"] * len(ids))
            c.execute(f"DELETE FROM vod_conversations WHERE agent_session_id IN ({ph})", tuple(ids))
        elif repo_key:
            c.execute("DELETE FROM vod_conversations WHERE agent_session_id IN "
                      "(SELECT id FROM devops_agent_session WHERE repo_key=%s AND agent_type='vod')",
                      (repo_key,))
        else:
            return 0
        return int(c.rowcount or 0)


def detach_agent_sessions(runtime_ids: list[int] | None = None) -> int:
    """把引用被删/被换引擎的 agent 会话摘绑（runtime_id=0）。"""
    rids = [int(r) for r in (runtime_ids or [])]
    if not rids:
        return 0
    ph = ",".join(["%s"] * len(rids))
    with db_registry.cursor("runtime") as c:
        c.execute(f"UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN ({ph})",
                  tuple(rids))
        return int(c.rowcount or 0)


def clear_agent_session_runtime(repo_key: str, agent_type: str) -> int:
    """清掉某仓库某 agent 的 runtime_session_id（重开会话用）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE devops_agent_session SET runtime_session_id='' "
                  "WHERE repo_key=%s AND agent_type=%s", (repo_key, agent_type))
        return int(c.rowcount or 0)


def ensure_agent_session(session_id: str, repo_key: str, agent_type: str,
                         status: str = "idle") -> int:
    """幂等建 agent 会话行（已存在则不动），返回 1=新建 / 0=已存在。"""
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT IGNORE INTO devops_agent_session "
                  "(id, repo_key, agent_type, status, last_active_at) "
                  "VALUES (%s, %s, %s, %s, NOW())",
                  (str(session_id), repo_key, agent_type, status))
        return int(c.rowcount or 0)


def stamp_task_traces(pairs: list[tuple]) -> int:
    """批量补写 agent_tasks 的 trace_id / pipeline_trace_id（insight 回填，幂等）。

    pairs: [(task_id, trace_id, pipeline_trace_id), ...]
    """
    if not pairs:
        return 0
    done = 0
    with db_registry.cursor("runtime") as c:
        for tid, tid_trace, ptrace in pairs:
            try:
                c.execute("UPDATE agent_tasks SET trace_id=%s, pipeline_trace_id=%s WHERE id=%s",
                          (tid_trace, ptrace, str(tid)))
                done += int(c.rowcount or 0)
            except Exception:  # noqa: BLE001
                continue
    return done


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("runtime") as c:
            c.execute("SELECT COUNT(*) FROM agent_runtimes")
            n = c.fetchone()[0]
        return {"ok": True, "runtimes": n, "dsn_configured": bool(db_registry.dsn_of("runtime"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("runtime"))}


__all__ = ["available", "list_runtimes", "list_runtimes_rows", "get_runtime",
           "task_stats", "append_task_message", "aggregate_counts", "get_skill_by_id",
           "get_skill_content_hash", "count_skills", "latest_task_for_event",
           "task_brief", "list_credential_rows", "list_repo_credentials", "has_engine_capability", "list_engine_capabilities",
           "list_engine_tags", "query_events", "count_events", "group_events", "get_autopilot_execution", "upsert_engine_capability",
           "delete_engine_capability", "update_engine_capability", "renew_channel_lease", "acquire_channel_lease", "release_channel_lease",
           "release_all_leases", "heartbeat_instance", "list_active_instances", "health",
           # P3-5g 写原语（mgmt / insight 进程写 runtime 基表）
           "purge_user_rows", "delete_chat_conversations", "delete_vod_conversations",
           "detach_agent_sessions", "clear_agent_session_runtime", "ensure_agent_session",
           "stamp_task_traces"]