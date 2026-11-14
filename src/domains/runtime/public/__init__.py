"""runtime 域对外 lib（进程内 import，使用 runtime 自己的 DSN）。

约定（见 plan-lib-three-node §11.3，为将来切 Remote 预留）：
- 只返回 JSON 友好类型（dict/list/基础类型），不返回游标/行对象
- 列表函数支持批量或 limit/offset
- 写函数接受 idempotency_key（最终一致 + 补偿，非强事务）
"""
from __future__ import annotations
from src.kernel.config import log

import json
from contextlib import contextmanager
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
    with _any_cursor() as c:
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
    """仓库凭据行（只返回密文 value 列，解密归调用方）。

    表归 **insight**（不是 runtime）：SQL 在该域 public 一份，本函数只做转发。
    """
    from src.domains.insight import public as _insight_cred

    return _insight_cred.list_repo_credential_rows(repo_key)


_EC_COLS = ("id", "runtime_id", "tag", "max_concurrent", "priority", "enabled",
            "created_at", "updated_at")
_ASK_COLS = ("id", "scope", "repo_key", "agent_type", "issue_num", "file_path", "status",
             "source_seq", "created_by", "created_at", "obs_key", "content_hash", "updated_at",
             "source", "archived", "description", "name", "org_id")


def has_engine_capability(runtime_id: int) -> bool:
    """该引擎是否有能力记录（mgmt 侧用 SELECT 1 判定）。"""
    with _any_cursor() as c:
        c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (int(runtime_id),))
        return c.fetchone() is not None


def list_engine_capabilities(runtime_id: int, enabled_only: bool = False) -> list[tuple]:
    """引擎能力行（id, tag, priority, enabled），按 tag 排序。"""
    sql = "SELECT id, tag, priority, enabled FROM engine_capabilities WHERE runtime_id=?"
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY tag"
    with _any_cursor() as c:
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
    """写入/更新引擎能力（幂等 UPSERT，供跨域写补偿重放）。

    可移植两段式（UPDATE → rowcount==0 → INSERT）：原实现是 MySQL 专用
    `ON DUPLICATE KEY UPDATE ... VALUES(x)`，在 sqlite 下非法；两段式覆盖同样的三列，
    语义等价且不依赖方言，两条拓扑共用。
    """
    rid, tg = int(runtime_id), tag
    mc, pri, en = int(max_concurrent), int(priority), int(enabled)
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE engine_capabilities SET max_concurrent=?, priority=?, enabled=?, "
            "updated_at=datetime('now') WHERE runtime_id=? AND tag=?", (mc, pri, en, rid, tg))
        if not int(getattr(cur, "rowcount", 0) or 0):
            c.execute(
                "INSERT INTO engine_capabilities "
                "(runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
                (rid, tg, mc, pri, en))



def update_engine_capability(runtime_id: int, tag: str, priority: int, enabled: int) -> int:
    """更新引擎能力优先级/启用态，返回受影响行数（0 = 不存在，供 404 判定）。"""
    with _any_cursor(write=True) as c:
        c.execute("UPDATE engine_capabilities SET priority=?, enabled=?, updated_at=datetime('now') "
                  "WHERE runtime_id=? AND tag=?",
                  (int(priority), int(enabled), int(runtime_id), tag))
        return int(c.rowcount or 0)


def delete_engine_capability(runtime_id: int, tag: str | None = None) -> int:
    """删除引擎能力（tag 为空则删该引擎全部），返回受影响行数；天然幂等。"""
    with _any_cursor(write=True) as c:
        if tag:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag=?",
                      (int(runtime_id), tag))
        else:
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (int(runtime_id),))
        return int(c.rowcount or 0)


def _mgmt_channel_lib():
    """W2 渠道表（mgmt 归属）的 lib 句柄：可用则写 owner 库，否则返回 None 走原 SQL。

    为什么 foreign_only：mgmt 进程内也挂载了 runtime 路由，同域写必须留在本地事务里。
    """
    from src.kernel import domain_read

    return domain_read.mgmt_write_lib(foreign_only=True)


def acquire_channel_lease(lease_key: str, token: str, owner: str = "",
                        ttl_seconds: int = 60) -> bool:
    """抢占渠道租约（CAS + 过期回收），可重放。

    条件：租约为空、已过期，或本来就属于自己（同 token 重入）。
    行不存在时插入（并发插入由 lease_key 主键保证只有一方成功）。
    """
    # 表归 mgmt：租约 CAS 原语在该域 public（任意拓扑，单库回落进程库）
    from src.domains.mgmt import public as _mgmt_lease

    return bool(_mgmt_lease.acquire_channel_lease(lease_key, token, owner, ttl_seconds))


def release_channel_lease(lease_key: str, token: str) -> bool:
    """释放租约：仅当 token 匹配时清空（幂等：已释放则返回 False）。"""
    # 表归 mgmt：释放原语在该域 public（任意拓扑）
    from src.domains.mgmt import public as _mgmt_lease

    return bool(_mgmt_lease.release_channel_lease(lease_key, token))


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

    # 表归 mgmt：续约 CAS 在 owner 域 public（任意拓扑）
    from src.domains.mgmt import public as _mgmt_lease

    return bool(_mgmt_lease.renew_channel_lease(lease_key, token, owner, ttl_seconds,
                                                expected_token))


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
            except Exception as _e:
                log.debug(f"src/domains/runtime/public/__init__.py best-effort skipped: {_e}")
                continue
    return done


# ---------- 杂项写原语（P3-5h：mgmt 进程写 runtime 基表） ----------

def mark_detach_rebound(scene_row_id: int, node_tag: str, rebound_at: str) -> int:
    """把某场景节点的解绑审计行标记为已重建（只动未标过的行）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE scene_engine_detach_log SET rebound_at=%s "
                  "WHERE scene_row_id=%s AND node_tag=%s "
                  "AND (rebound_at='' OR rebound_at IS NULL)",
                  (rebound_at, int(scene_row_id), node_tag))
        return int(c.rowcount or 0)


def delete_autopilot_executions(task_id: int) -> int:
    """删除某巡检任务的执行记录。"""
    with db_registry.cursor("runtime") as c:
        c.execute("DELETE FROM autopilot_executions WHERE task_id=%s", (int(task_id),))
        return int(c.rowcount or 0)


def set_autopilot_findings(exec_id: int, findings_json: str) -> int:
    """回写某次巡检的 findings_json（确认标记/派发号）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE autopilot_executions SET findings_json=%s WHERE id=%s",
                  (findings_json, int(exec_id)))
        return int(c.rowcount or 0)


def set_repo_im_notifications(auto_impl_enabled: int, notify_enabled: int) -> int:
    """更新通知偏好（原 SQL 无 WHERE，整表更新 —— 与原实现一致）。"""
    with _any_cursor(write=True) as c:
        c.execute("UPDATE repo_im_group_binding SET auto_impl_enabled=?, notify_enabled=?",
                  (int(auto_impl_enabled), int(notify_enabled)))
        return int(c.rowcount or 0)


_ACTION_FILTER_WRITABLE = {"allow", "actor", "require_changes", "block_changes",
                           "handle_side_effect", "priority", "enabled"}


def add_event_action_filter(fields: dict[str, Any], created_at: str) -> int:
    """新增事件过滤规则，返回新行 id（调用方的 409 去重在读侧完成）。

    W2：表归 mgmt 域，lib 可用时委托 mgmt public 写函数。
    """
    # 表归 mgmt：写入原语在 owner 域 public（任意拓扑）
    from src.domains.mgmt import public as _mgmt_af

    payload = dict(fields or {})
    payload["updated_at"] = created_at
    return int(_mgmt_af.add_event_action_filter_row(payload, created_at))


def update_event_action_filter(rule_id: int, fields: dict[str, Any], updated_at: str) -> int:
    """按白名单列更新事件过滤规则；返回受影响行数（0 = 不存在，供 404 判定）。

    W2：表归 mgmt 域，lib 可用时委托 mgmt public 写函数。
    """
    unknown = [k for k in fields if k not in _ACTION_FILTER_WRITABLE]
    if unknown:
        raise ValueError(f"column(s) {unknown} not writable through runtime lib")
    # 表归 mgmt：写入原语在 owner 域 public（任意拓扑）
    from src.domains.mgmt import public as _mgmt_af

    data = {k: v for k, v in fields.items() if k in _ACTION_FILTER_WRITABLE}
    return int(_mgmt_af.update_event_action_filter_row(rule_id, data, updated_at))


def delete_event_action_filter(rule_id: int) -> int:
    """删除规则（W2：表归 mgmt，lib 可用时委托 mgmt public 写函数）。"""
    # 表归 mgmt：删除原语在 owner 域 public（任意拓扑）
    from src.domains.mgmt import public as _mgmt_af

    return int(_mgmt_af.delete_event_action_filter_row(rule_id))


# ---------- 技能写原语（P3-5f：mgmt 进程写 runtime 基表 skill） ----------

def update_skill_content(skill_id: int, content_hash: str, file_path: str = "") -> int:
    """更新技能内容哈希（惰性：file_path 为空则只改 hash）。"""
    with db_registry.cursor("runtime") as c:
        if file_path:
            c.execute("UPDATE skill SET content_hash=%s, file_path=%s WHERE id=%s",
                      (content_hash, file_path, int(skill_id)))
        else:
            c.execute("UPDATE skill SET content_hash=%s WHERE id=%s",
                      (content_hash, int(skill_id)))
        return int(c.rowcount or 0)


def update_skill_obs(skill_id: int, object_key: str, display_name: str, agent_type: str,
                     description: str, category_id: str, updated_by: str,
                     content_hash: str) -> int:
    """OBS 同步后更新技能元数据（与原 SQL 同列集，updated_at=NOW()）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE skill SET file_path=%s, display_name=%s, agent_type=%s, "
                  "description=%s, category_id=%s, updated_by=%s, is_active=1, "
                  "content_hash=%s, updated_at=NOW() WHERE id=%s",
                  (object_key, display_name, agent_type, description, category_id,
                   updated_by, content_hash, int(skill_id)))
        return int(c.rowcount or 0)


def insert_skill(skill_name: str, display_name: str, description: str, agent_type: str,
                 file_path: str, scope: str, owner_id: str, created_by: str,
                 updated_by: str, category_id: str = "", content_hash: str = "") -> int:
    """新建技能行（OBS 路径），返回新行 id。"""
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, "
                  "file_path, scope, owner_id, created_by, updated_by, is_active, "
                  "category_id, content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)",
                  (skill_name, display_name, description, agent_type, file_path, scope,
                   owner_id, created_by, updated_by, category_id, content_hash))
        return int(c.lastrowid or 0)


def create_skill_row(skill_name: str, description: str, agent_type: str, scope: str,
                     owner_id: str, created_by: str) -> int:
    """幂等建技能行（skill_path=skill_name），返回 1=新建 / 0=已存在。"""
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT IGNORE INTO skill (skill_name, display_name, description, agent_type, "
                  "skill_path, scope, owner_id, created_by, updated_by) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (skill_name, skill_name, description, agent_type, skill_name, scope,
                   owner_id, created_by, created_by))
        return int(c.rowcount or 0)


def delete_skill_row(skill_name: str, scope: str, owner_id: str) -> int:
    """按 (skill_name, scope, owner_id) 删除技能行。"""
    with db_registry.cursor("runtime") as c:
        c.execute("DELETE FROM skill WHERE skill_name=%s AND scope=%s AND owner_id=%s",
                  (skill_name, scope, owner_id))
        return int(c.rowcount or 0)


def delete_skill_by_id(skill_id: int) -> int:
    """按主键删除技能行。"""
    with db_registry.cursor("runtime") as c:
        c.execute("DELETE FROM skill WHERE id=%s", (int(skill_id),))
        return int(c.rowcount or 0)


def adjust_skill_like_count(skill_id: int, delta: int) -> int:
    """点赞计数增减（delta 正负决定 +1/-1，原子自增避免读改写丢更新）。"""
    op = "+" if int(delta) >= 0 else "-"
    with db_registry.cursor("runtime") as c:
        c.execute(f"UPDATE skill SET like_count=like_count{op}1 WHERE id=%s", (int(skill_id),))
        return int(c.rowcount or 0)


# ---------- 凭据写原语（P3-5f-3：mgmt 进程写 runtime 基表） ----------

def insert_runtime_credential(token_hash: str, name: str, scope: str, owner_id: str,
                              owner_name: str, repo_key: str, org_id: str) -> dict[str, Any]:
    """新建 runtime 凭据。名称全局唯一：撞唯一约束时返回 duplicate（**不抛异常**），
    调用方据此回 400 —— 若这里抛异常，意图会被当成可重试失败而无限重放。"""
    with _any_cursor(write=True) as c:
        try:
            c.execute("INSERT INTO runtime_credentials "
                      "(token_hash, name, scope, owner_id, owner_name, repo_key, org_id) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (token_hash, name, scope, owner_id, owner_name, repo_key, org_id))
            return {"action": "inserted", "id": int(c.lastrowid or 0)}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "UNIQUE" in msg or "1062" in msg or "Duplicate" in msg:
                return {"action": "duplicate", "id": 0}
            raise


def rename_runtime_credential(cid: str, name: str) -> int:
    """改名（仅 active 行），返回受影响行数。"""
    with _any_cursor(write=True) as c:
        c.execute("UPDATE runtime_credentials SET name=? WHERE id=? AND status='active'",
                  (name, str(cid)))
        return int(c.rowcount or 0)


def set_runtime_credential_scope(cid: str, scope: str, repo_key: str, owner_id: str,
                                 org_id: str) -> int:
    """一次性同步 scope / repo_key / owner_id / org_id。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE runtime_credentials SET scope=%s, repo_key=%s, owner_id=%s, org_id=%s "
                  "WHERE id=%s", (scope, repo_key, owner_id, org_id, str(cid)))
        return int(c.rowcount or 0)


def delete_runtime_credential(cid: str) -> int:
    """删除凭据行。"""
    with _any_cursor(write=True) as c:
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (str(cid),))
        return int(c.rowcount or 0)


def credential_health_rows_any() -> list[tuple]:
    """全部未吊销凭据的 (name, status, last_seen_at)（任意拓扑）。

    原语句在 mgmt 进程库里直查 runtime 表：三库拓扑下该库没有 runtime_credentials
    （视图 0 → 1146，被调用方 except 吞成「全部 offline」），DevStage 环境列表恒显示
    未上线。口径与原实现一致：只排除 `status='revoked'`，**不**过滤 `is_active`/空名，
    空 name 行交调用方跳过（原循环里的 `if not cname: continue`）。
    """
    return [tuple(r) for r in read_all_any("credential_health_rows")]


def credential_id_by_name_any(name: str) -> str | None:
    """按凭据名取 id（任意拓扑）。名称语义上唯一，与原实现一样只取第一行。

    不做 strip/normalize：原语句就是 `WHERE name=?` 逐字匹配；空名直接返回 None
    （避免把 `name=''` 的脏行当成命中而误删）。
    """
    _name = name or ""
    if not _name:
        return None
    row = read_one_any("credential_id_by_name", (_name,))
    return str(row[0]) if row else None


def purge_env_credential_any(env_name: str) -> dict[str, Any]:
    """按凭据名清理 DevStage 环境的 runtime 半区（任意拓扑 + 域内事务）。

    与 `purge_runtime_credential` 同一口径：摘绑会话 → 删能力 → 删引擎行 → 删凭据行；
    入口从凭据 id 换成凭据名（mgmt 侧只持有 env.name，创建环境时 name = env.name）。
    返回 {found, cid, rids, sessions, caps, engines, deleted}；`rids` 交回调用方清
    mgmt 侧 `devops_agent_runtime_binding`（跨域边界不把 mgmt 表名传进来）。
    凭据不存在时 found=False 且**不做任何写**（原实现同样只在命中后才动手）。
    """
    out: dict[str, Any] = {"found": False, "cid": "", "rids": [], "sessions": 0,
                           "caps": 0, "engines": 0, "deleted": 0}
    cid = credential_id_by_name_any(env_name)
    if not cid:
        return out
    out["found"] = True
    out["cid"] = cid
    out.update(purge_runtime_credential(cid))
    return out


def upsert_repo_credential(repo_key: str, cred_type: str, value: str,
                           updated_at: str) -> dict[str, Any]:
    """仓库凭据幂等 upsert（表归 insight，写原语在该域 public；返回 {action, id}）。"""
    from src.domains.insight import public as _insight_cred

    return _insight_cred.upsert_repo_credential_value(repo_key, cred_type, value, updated_at)


def clear_repo_credential(repo_key: str, cred_type: str, updated_at: str) -> int:
    """清空仓库凭据值（value=''，保留行；表归 insight）。"""
    from src.domains.insight import public as _insight_cred

    return _insight_cred.clear_repo_credential_value(repo_key, updated_at, cred_type)


def ensure_device_credential(hostname: str, token_hash: str, last_ip: str) -> dict[str, Any]:
    """本机指标上报的 device 凭据行（scope='global'）幂等登记。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id FROM runtime_credentials WHERE device_name=%s AND scope='global' LIMIT 1",
                  (hostname,))
        row = c.fetchone()
        if row:
            return {"action": "exists", "id": int(row[0])}
        try:
            c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, last_ip) "
                      "VALUES (%s,%s,%s,'global','active',%s)",
                      (token_hash, hostname, hostname, last_ip))
            return {"action": "inserted", "id": int(c.lastrowid or 0)}
        except Exception as exc:  # noqa: BLE001
            if "UNIQUE" in str(exc) or "1062" in str(exc) or "Duplicate" in str(exc):
                return {"action": "duplicate", "id": 0}
            raise


def touch_credential_metrics(hostname: str, last_seen_at: str, last_ip: str = "",
                             cpu_percent: float = 0, memory_percent: float = 0,
                             disk_percent: float = 0, tmp_percent: float = 0,
                             bandwidth_percent: float = 0, bandwidth_mbps: float = 0,
                             load_avg: str = "") -> int:
    """刷新 device 凭据的在线指标（15s 心跳；同值重放无副作用）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE runtime_credentials SET last_seen_at=%s, last_ip=%s, cpu_percent=%s, "
                  "memory_percent=%s, disk_percent=%s, tmp_percent=%s, bandwidth_percent=%s, "
                  "bandwidth_mbps=%s, load_avg=%s WHERE device_name=%s AND scope='global'",
                  (last_seen_at, last_ip, cpu_percent, memory_percent, disk_percent, tmp_percent,
                   bandwidth_percent, bandwidth_mbps, load_avg, hostname))
        return int(c.rowcount or 0)


# ---------- 引擎行写原语（P3-5f-2：mgmt 进程写 runtime 基表 agent_runtimes） ----------

def upsert_local_runtimes(agents: list[dict], scope: str, owner_id: str, host_name: str,
                          now_iso: str) -> dict[str, Any]:
    """本机直连引擎行批量 upsert：SELECT → UPDATE/INSERT → prune → dedupe，**同一事务**。

    一次调用 = 一次"本机探测即快照"，所以不能拆成多条意图（旧 payload 重放会误删新行）。
    顺带修掉两个既有缺陷：
      · 旧实现在 mgmt 事务未提交时用**另一条连接**读 engine_capabilities → 读不到刚插的行，
        `agent_tags` 被写成 `[]`；这里在同一事务内读，tags 正确。
      · health 派生改用**本次探测的新值**（installed/healthy），而不是上一次的旧值。
    """
    import json as _json

    from src.domains.runtime.engine.health import derive_local_runtime_health

    out: dict[str, Any] = {"upserted": 0, "inserted": 0, "pruned": 0, "deduped": 0, "ids": {}}
    names: list[str] = []
    with _any_tx() as c:  # 任意拓扑：自有 DSN 真事务；单库回落进程库 + db_lock
        try:
            c.execute("SET SESSION innodb_lock_wait_timeout=5")
        except Exception as _e:
            log.debug(f"src/domains/runtime/public/__init__.py best-effort skipped: {_e}")
        for a in agents or []:
            name = (a.get("name") or "").strip().lower()
            if not name:
                continue
            names.append(name)
            _ls = _json.dumps(a.get("local_skills") or [], ensure_ascii=False)
            _detail = a.get("local_skills_detail")
            _detail_json = _json.dumps(_detail, ensure_ascii=False) if _detail is not None else ""
            c.execute(
                "SELECT id FROM agent_runtimes WHERE name=? AND scope=? AND owner_id=? "
                "AND credential_id=0 AND (host_name=? OR host_name='' OR host_name IS NULL) "
                "ORDER BY CASE WHEN host_name=? THEN 0 ELSE 1 END, id LIMIT 1",
                (name, scope, owner_id, host_name, host_name))
            row = c.fetchone()
            if row:
                rid = int(row[0])
                _derived = derive_local_runtime_health(bool(a.get("installed")), bool(a.get("healthy")))
                try:
                    c.execute("UPDATE agent_runtimes SET status='online', last_seen_at=?, health=? "
                              "WHERE id=?", (now_iso, _derived, rid))
                except Exception:  # noqa: BLE001 - 无遗留 health 列的库
                    c.execute("UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id=?",
                              (now_iso, rid))
                c.execute(
                    "UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, "
                    "healthy=?, pip_package=?, updated_at=?, local_skills=?, "
                    "skill_scan_result=?, host_name=? WHERE id=?",
                    (a.get("display_name", name), 1 if a.get("installed") else 0,
                     a.get("cli_path", ""), a.get("version", ""),
                     1 if a.get("healthy") else 0, a.get("pip_package", ""), now_iso,
                     _ls, _detail_json, host_name, rid))
                out["upserted"] += 1
                out["ids"][name] = rid
                continue
            if not a.get("installed"):
                continue
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, "
                "pip_package, scope, owner_id, local_skills, skill_scan_result, host_name, "
                "last_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, a.get("display_name", name), 1, a.get("cli_path", ""), a.get("version", ""),
                 1 if a.get("healthy") else 0, a.get("pip_package", ""), scope, owner_id,
                 _ls, _detail_json, host_name, now_iso, now_iso))
            rid = int(c.lastrowid or 0)
            c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (rid,))
            if not c.fetchone():
                c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, "
                          "enabled, created_at, updated_at) VALUES (?,'all',1,50,1,?,?)",
                          (rid, now_iso, now_iso))
            c.execute("SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag",
                      (rid,))
            tags = [r[0] for r in c.fetchall()]
            c.execute("UPDATE agent_runtimes SET agent_tags=? WHERE id=?",
                      (_json.dumps(tags, ensure_ascii=False), rid))
            out["inserted"] += 1
            out["ids"][name] = rid

        if names:
            ph = ",".join(["?"] * len(names))
            c.execute(f"DELETE FROM agent_runtimes WHERE scope=? AND owner_id=? AND credential_id=0 "
                      f"AND host_name=? AND lower(name) NOT IN ({ph})",
                      (scope, owner_id, host_name, *names))
            out["pruned"] = int(c.rowcount or 0)
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND id NOT IN "
                  "(SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 "
                  "GROUP BY lower(name), scope, owner_id, COALESCE(org_id,0), COALESCE(host_name,''))")
        out["deduped"] = int(c.rowcount or 0)
    return out


def ensure_default_runtime(name: str, display_name: str, installed: int, cli_path: str,
                           version: str, healthy: int, pip_package: str,
                           scope: str) -> dict[str, Any]:
    """确保存在默认引擎行（本机直连 credential_id=0），返回 {id, created}。"""
    with _any_tx() as c:  # 任意拓扑：自有 DSN 真事务；单库回落进程库 + db_lock
        c.execute("SELECT ar.id FROM agent_runtimes ar "
                  "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                  "WHERE lower(ar.name)=? AND ar.scope=? AND ar.owner_id='' "
                  "AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active') "
                  "ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC "
                  "LIMIT 1", (name, scope))
        row = c.fetchone()
        if row:
            return {"id": int(row[0]), "created": False}
        c.execute("INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, "
                  "pip_package, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,'')",
                  (name, display_name, int(installed), cli_path, version, int(healthy), pip_package, scope))
        return {"id": int(c.lastrowid or 0), "created": True}


def set_agent_tags(runtime_id: int, tags: list[str]) -> int:
    """回写 agent_runtimes.agent_tags（engine_capabilities 的 JSON 快照）。"""
    import json as _json
    with _any_cursor(write=True) as c:
        c.execute("UPDATE agent_runtimes SET agent_tags=? WHERE id=?",
                  (_json.dumps(list(tags or []), ensure_ascii=False), int(runtime_id)))
        return int(c.rowcount or 0)


def purge_runtime_credential(cid: str, detach_sessions: bool = True,
                            drop_capabilities: bool = True) -> dict[str, Any]:
    """吊销凭据：删凭据行 + 级联其引擎行（可选摘绑会话 / 删能力），**一个事务**。

    返回 {rids, sessions, caps, engines, deleted}；mgmt 侧还要清理场景 JSON，
    所以把 rids 交回调用方（跨域边界不传 mgmt 表名）。
    """
    cid_s = str(cid)
    out: dict[str, Any] = {"rids": [], "sessions": 0, "caps": 0, "engines": 0, "deleted": 0}
    with _any_tx() as c:
        c.execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid_s,))
        rids = [int(r[0]) for r in c.fetchall()]
        out["rids"] = rids
        if rids:
            ph = ",".join(["?"] * len(rids))
            if detach_sessions:
                c.execute(f"UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN ({ph})",
                          tuple(rids))
                out["sessions"] = int(c.rowcount or 0)
            if drop_capabilities:
                c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({ph})", tuple(rids))
                out["caps"] = int(c.rowcount or 0)
            c.execute(f"DELETE FROM agent_runtimes WHERE id IN ({ph})", tuple(rids))
            out["engines"] = int(c.rowcount or 0)
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid_s,))
        out["deleted"] = int(c.rowcount or 0)
    return out


def change_credential_scope(cid: str, scope: str, repo_key: str, owner_id: str,
                           org_id: str) -> dict[str, Any]:
    """切换凭据 scope：凭据行 + 其引擎行 + 同凭据重名引擎行合并，**一个事务**。

    合并口径与原 `merge_duplicate_engine_rows` 一致：按 (credential_id, lower(name)) 去重、
    保留 MAX(id)、迁移 capabilities 与 tags_user_set。返回 {credential, engines, merged}。
    """
    import json as _json  # noqa: F401  (保持与其它写原语同风格)

    cid_s = str(cid)
    out: dict[str, Any] = {"credential": 0, "engines": 0, "merged": {}}
    with _any_tx() as c:
        c.execute("UPDATE runtime_credentials SET scope=?, repo_key=?, owner_id=?, org_id=? "
                  "WHERE id=?", (scope, repo_key or None, owner_id, org_id, cid_s))
        out["credential"] = int(c.rowcount or 0)

        # ---- 合并同凭据下的重名引擎行（保留 MAX(id)）----
        rid_map: dict[int, int] = {}
        c.execute("SELECT id, lower(name), tags_user_set FROM agent_runtimes WHERE credential_id=?",
                  (cid_s,))
        groups: dict[str, list[tuple[int, int]]] = {}
        for rid, lname, tus in c.fetchall():
            groups.setdefault(str(lname or ""), []).append((int(rid), int(tus or 0)))
        replaced_ids: list[int] = []
        for _lname, items in groups.items():
            if len(items) < 2:
                continue
            items_sorted = sorted(items, key=lambda x: x[0])
            surviving = items_sorted[-1][0]
            replaced = [rid for rid, _ in items_sorted[:-1]]
            c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (surviving,))
            if not c.fetchone():
                for rid in replaced:
                    c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? LIMIT 1", (rid,))
                    if c.fetchone():
                        c.execute("UPDATE engine_capabilities SET runtime_id=?, "
                                  "updated_at=CURRENT_TIMESTAMP WHERE runtime_id=?", (surviving, rid))
                        break
            if items_sorted[-1][1] == 0 and any(tus for _, tus in items_sorted[:-1]):
                c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (surviving,))
            for rid in replaced:
                rid_map[rid] = surviving
            replaced_ids.extend(replaced)
        if replaced_ids:
            ph = ",".join(["?"] * len(replaced_ids))
            c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({ph})", tuple(replaced_ids))
            c.execute(f"DELETE FROM agent_runtimes WHERE id IN ({ph})", tuple(replaced_ids))
        out["merged"] = {str(k): v for k, v in rid_map.items()}

        c.execute("UPDATE agent_runtimes SET scope=?, owner_id=?, org_id=?, repo_key=? "
                  "WHERE credential_id=?", (scope, owner_id, org_id, repo_key or None, cid_s))
        out["engines"] = int(c.rowcount or 0)
    return out


_REASSIGN_TABLES = ("agent_runtimes", "runtime_credentials", "chat_conversations",
                    "devops_agent_session", "issue", "agent_tasks")


def reassign_repo_org_id(repo_key: str, org_id: str) -> dict[str, int]:
    """仓库迁移时把本域 4 张表的 org_id 一起改到目标组织（表名固定，不接受动态表名）。"""
    out: dict[str, int] = {}
    with db_registry.transaction("runtime") as c:
        for tbl in _REASSIGN_TABLES:
            try:
                c.execute(f"UPDATE {tbl} SET org_id=%s WHERE repo_key=%s", (org_id, repo_key))
                out[tbl] = int(c.rowcount or 0)
            except Exception as exc:  # noqa: BLE001 - 旧库缺列不致命（与原实现一致）
                out[tbl] = 0
                log_msg = f"reassign_repo_org_id({tbl}) skipped: {exc}"
                try:
                    import logging
                    logging.getLogger(__name__).warning(log_msg)
                except Exception as _e:
                    log.debug(f"src/domains/runtime/public/__init__.py best-effort skipped: {_e}")
    return out


# ---------- 命名查询白名单（P4 读路径：删视图后跨域读走 lib） ----------
# 为什么不是"任意 SQL 直通"：跨域读也要有边界。这里只登记**用到的具体查询**，
# 调用方传查询名 + 参数，未知查询名直接 KeyError —— 不给远端开任意读口子。
_READ_QUERIES: dict[str, str] = {
    # 引擎行
    "runtime_name_cred": "SELECT name, credential_id FROM agent_runtimes WHERE id=%s",
    "runtime_host_name": "SELECT host_name FROM agent_runtimes WHERE id=%s",
    "runtime_exists": "SELECT 1 FROM agent_runtimes WHERE id=%s LIMIT 1",
    "runtimes_by_credential": (
        "SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills, "
        "skill_scan_result, worker_version FROM agent_runtimes WHERE credential_id=%s"),
    "runtime_ids_by_credential": "SELECT id FROM agent_runtimes WHERE credential_id=%s",
    "runtime_ages": "SELECT id, last_seen_at, updated_at FROM agent_runtimes",
    # 能力标签
    "capability_tag_hit": (
        "SELECT 1 FROM engine_capabilities WHERE runtime_id=%s AND enabled=1 "
        "AND (tag=%s OR tag='all') LIMIT 1"),
    "engine_tags_enabled": "SELECT tag FROM engine_capabilities WHERE runtime_id=%s AND enabled=1 ORDER BY tag",
    "events_a24eb5": "SELECT seq, repo_key, issue_id, agent_type, status, runtime, result FROM events WHERE seq=%s",
    "skill_d40f5e": "SELECT id FROM skill WHERE skill_name=%s AND scope=%s AND is_active=1 LIMIT 1",
    "skill_1abf56": "SELECT display_name FROM skill WHERE skill_name=%s AND is_active=1 AND scope='official'",
    "skill_820107": "SELECT id, skill_name, scope FROM skill WHERE skill_name=%s AND is_active=1 ORDER BY CASE scope WHEN 'official' THEN 0 WHEN 'repo' THEN 1 ELSE 2 END LIMIT 1",
    "skill_8d3c93": "SELECT skill_name, skill_path, scope FROM skill WHERE id=%s AND is_active=1",
    "skill_21ba32": "SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id FROM skill WHERE skill_name=%s AND scope='repo' AND owner_id=%s AND is_active=1 LIMIT 1",
    "skill_9f2ee7": "SELECT skill_name, scope, owner_id, agent_type, category_id FROM skill WHERE id=%s AND is_active=1",
    "skill_f41425": "SELECT skill_name, owner_id, skill_path, scope FROM skill WHERE id=%s AND is_active=1",
    "skill_ded9f4": "SELECT id, skill_name, display_name, description, agent_type, category_id FROM skill WHERE is_active=1 AND scope='official' ORDER BY agent_type, skill_name",
    "skill_4e3919": "SELECT id, skill_name, display_name, description, agent_type, category_id FROM skill WHERE is_active=1 AND scope='official' AND (skill_name=%s OR display_name=%s) ORDER BY skill_name",
    "skill_43d037": "SELECT id, skill_name, display_name, description, agent_type, skill_path, file_path, scope, owner_id, content_hash FROM skill WHERE agent_type=%s AND is_active=1 AND scope='official' ORDER BY id",
    "skill_652318": "SELECT id FROM skill WHERE skill_name=%s AND scope='user' AND owner_id=%s",
    "skill_311fe3": "SELECT id, owner_id FROM skill WHERE skill_name=%s AND scope='user' AND is_active=1 LIMIT 1",
    "skill_94e35b": "SELECT id FROM skill WHERE skill_name=%s AND scope='official' AND is_active=1 LIMIT 1",
    "skill_015f53": "SELECT id, owner_id FROM skill WHERE skill_name=%s AND scope='repo' AND owner_id=%s AND is_active=1 LIMIT 1",
    "skill_4a8bb9": "SELECT id FROM skill WHERE skill_name=%s AND scope='repo' AND owner_id=%s AND is_active=1 LIMIT 1",
    "skill_17c0d2": "SELECT id FROM skill WHERE skill_name=%s AND scope='user' AND owner_id=%s AND is_active=1 LIMIT 1",
    "skill_da7be4": "SELECT id, content_hash, file_path, is_active FROM skill WHERE id=%s",
    "skill_1b908c": "SELECT id, content_hash, file_path FROM skill WHERE skill_name=%s AND scope=%s LIMIT 1",
    "skill_4b5014": "SELECT id, skill_name, display_name, description, agent_type, skill_path, file_path, scope, owner_id, content_hash FROM skill WHERE id=%s AND is_active=1",
    "agent_runtimes_1c267b": "SELECT model_name FROM agent_runtimes WHERE id=%s",
    "agent_runtimes_7776a6": "SELECT COALESCE(NULLIF(r.name,''), NULLIF(r.provider,''), ''), COALESCE(rc.device_name, rc.name, '') FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id WHERE r.id=%s",
    "agent_runtimes_34b97d": "SELECT name, status, healthy FROM agent_runtimes WHERE status='online' AND healthy=1 ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1",
    "agent_runtimes_a4f724": "SELECT ar.name, ar.credential_id, COALESCE(rc.device_name,''), COALESCE(rc.last_ip,''), COALESCE(ar.provider,'') FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id WHERE ar.id=%s",
    "agent_runtimes_5fd717": "SELECT status, healthy FROM agent_runtimes WHERE id=%s",
    "agent_runtimes_62d4e0": "SELECT r.credential_id, rc.scope, rc.owner_id FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=%s",
    "agent_runtimes_e6e235": "SELECT MIN(updated_at) FROM agent_runtimes",
    "agent_runtimes_808985": "SELECT ar.id FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id WHERE lower(ar.name)=%s AND ar.scope=%s AND ar.owner_id='' AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active') ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC LIMIT 1",
    "agent_runtimes_145cfe": "SELECT ar.id FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id WHERE lower(ar.name)=%s AND ar.scope=%s AND ar.owner_id=%s AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active') ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC LIMIT 1",
    "agent_runtimes_b84e88": "SELECT id FROM agent_runtimes WHERE lower(name)=%s AND credential_id=0 AND installed=1 ORDER BY id LIMIT 1",
    "agent_runtimes_874081": "SELECT id FROM agent_runtimes WHERE name=%s AND scope=%s AND owner_id=%s AND credential_id=0 AND (host_name=%s OR host_name='' OR host_name IS NULL) ORDER BY CASE WHEN host_name=%s THEN 0 ELSE 1 END, id LIMIT 1",
    "runtime_credentials_619821": "SELECT rc.id, rc.device_name, rc.cpu_percent, rc.memory_percent, rc.disk_percent, rc.bandwidth_percent, COALESCE(hs.health_score, 0), rc.status, rc.last_seen_at FROM runtime_credentials rc LEFT JOIN runtime_health_scores hs ON hs.runtime_id = rc.id WHERE rc.status != 'revoked'",
    "runtime_credentials_f8ea2f": "SELECT name FROM runtime_credentials WHERE id=%s",
    "runtime_credentials_911d7f": "SELECT name, device_name FROM runtime_credentials WHERE id=%s",
    "runtime_credentials_aff203": "SELECT scope, owner_name FROM runtime_credentials WHERE id=%s",
    "runtime_credentials_7431a6": "SELECT id FROM runtime_credentials WHERE name=%s",
    "runtime_credentials_3cd287": "SELECT owner_name FROM runtime_credentials WHERE id=%s",
    "runtime_credentials_ff9c0b": "SELECT owner_name, status FROM runtime_credentials WHERE id=%s",
    "runtime_credentials_5b448f": "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, last_ip, repo_key, org_id, spec FROM runtime_credentials",
    "devops_agent_session_dcd716": "SELECT id FROM devops_agent_session WHERE repo_key=%s AND agent_type='vod'",
    "devops_agent_session_a36e5f": "SELECT runtime_id FROM devops_agent_session WHERE repo_key=%s AND agent_type=%s",
    "agent_tasks_29ab8e": "SELECT repo_key, agent_type, runtime_id, user_id, issue_id FROM agent_tasks WHERE id=%s",
    "distributed_locks_4e8732": "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=%s",
    "agent_tasks_3b1cff": "SELECT trace_id, pipeline_trace_id FROM agent_tasks WHERE id=%s",
    "agent_tasks_3e8d71": "SELECT id, repo_key, issue_id FROM agent_tasks WHERE trace_id IS NULL OR trace_id='' LIMIT %s",
    "agent_type_7eae90": "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, enable FROM agent_type ORDER BY order_num, agent_type",
    "agent_type_b07f0c": "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, enable FROM agent_type WHERE agent_type=%s",
    "autopilot_executions_018f03": "SELECT findings_json, result FROM autopilot_executions WHERE id=%s",
    "agent_tasks_b6b789": "SELECT event_seq, agent_type, skill, runtime_id, repo_key FROM agent_tasks WHERE id=%s LIMIT 1",
    "agent_tasks_6e9804": "SELECT issue_id, repo_key FROM agent_tasks WHERE id=%s LIMIT 1",
    "engine_capabilities_440c87": "SELECT id, tag, priority, enabled, created_at, updated_at FROM engine_capabilities WHERE runtime_id=%s ORDER BY tag",
    # issue 查 id（mgmt feedback 上下文）
    "issue_id_by_repo_number": "SELECT id FROM issue WHERE repo_key=%s AND issue_number=%s LIMIT 1",
    # 仓库 IM 开关（mgmt 的 _apply_ak_sk 读）
    "repo_im_flags_latest": (
        "SELECT auto_impl_enabled, notify_enabled FROM repo_im_group_binding "
        "ORDER BY id DESC LIMIT 1"),
    # 账号 / 外部身份：解析归 mgmt 域 public（mapped_user_id / external_user_id），
    # 本白名单不再登记跨域读 SQL
}


def read_one(query: str, params: tuple = ()) -> Any | None:
    """按白名单执行单行读；未知查询名抛 KeyError（不静默）。"""
    sql = _READ_QUERIES.get(query)
    if sql is None:
        raise KeyError(f"unknown read query: {query}")
    with db_registry.cursor("runtime") as c:
        c.execute(sql, params)
        return c.fetchone()


def read_all(query: str, params: tuple = ()) -> list[Any]:
    """按白名单执行多行读；未知查询名抛 KeyError。"""
    sql = _READ_QUERIES.get(query)
    if sql is None:
        raise KeyError(f"unknown read query: {query}")
    with db_registry.cursor("runtime") as c:
        c.execute(sql, params)
        return list(c.fetchall() or [])


# ---------- P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12） ----------

# N1 列表返回列：不含 file_path（OBS 明文地址）与 owner 敏感列
_SKILL_LIST_COLS = ("id", "skill_name", "display_name", "description", "agent_type",
                    "scope", "owner_id", "category_id", "is_active", "is_featured",
                    "like_count", "updated_at", "org_id")


def list_skills(repo_key: str = "", q: str = "", limit: int = 50, offset: int = 0,
                active_only: bool = True) -> list[dict[str, Any]]:
    """技能列表（N1）：支持 repo_key / q / 分页；不返回 OBS 原件地址。

    `repo_key` 作用域映射：`scope='official'` 全仓可见；`scope`/`owner_id` 等于 repo_key
    的回车私有技能。上限由调用方 limit 控制（服务端 clamp 到 500）。
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    sql = f"SELECT {', '.join(_SKILL_LIST_COLS)} FROM skill WHERE 1=1"
    args: list[Any] = []
    if active_only:
        sql += " AND is_active=1"
    if q:
        like = f"%{q}%"
        sql += " AND (skill_name LIKE %s OR display_name LIKE %s OR description LIKE %s)"
        args += [like, like, like]
    if repo_key:
        sql += " AND (scope='official' OR owner_id=%s OR scope=%s)"
        args += [repo_key, repo_key]
    sql += " ORDER BY is_featured DESC, id DESC LIMIT %s OFFSET %s"
    args += [limit, offset]
    with db_registry.cursor("runtime") as c:
        c.execute(sql, tuple(args))
        return _rows(c)


def get_skill_detail(skill_id: int, url_expires: int = 300) -> dict[str, Any] | None:
    """技能详情（N2）：字段同 N1 + `content_url`（短时效签名，无存储则空串）。"""
    row = get_skill_by_id(int(skill_id))
    if not row:
        return None
    out = {k: row.get(k) for k in _SKILL_LIST_COLS if k in row}
    out["id"] = row.get("id")
    out["summary"] = row.get("description") or ""
    out["content_url"] = ""
    key = row.get("file_path") or ""
    if key:
        try:
            from src.domains.runtime.storage import get_storage

            storage = get_storage()
            if storage is not None:
                out["content_url"] = storage.temporary_url(key, int(url_expires))
        except Exception as exc:  # noqa: BLE001 - 存储不可用不等于接口失败
            log.warning(f"skill content_url unavailable: {exc}")
    return out


def list_all_engine_capabilities(limit: int = 500, enabled_only: bool = False) -> list[dict[str, Any]]:
    """引擎×能力矩阵一次全量（N3，< 500 行）。"""
    sql = ("SELECT id, runtime_id, tag, max_concurrent, priority, enabled, updated_at "
           "FROM engine_capabilities")
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY runtime_id, priority DESC, tag LIMIT %s"
    with db_registry.cursor("runtime") as c:
        c.execute(sql, (max(1, min(int(limit), 500)),))
        return _rows(c)


def get_runtime_credential_meta(runtime_id: int) -> dict[str, Any] | None:
    """运行凭据元数据（N4）：**绝不返回 token_hash / 明文**。"""
    sql = ("SELECT rc.id, rc.name, rc.device_name, rc.scope, rc.owner_id, rc.owner_name, "
           "rc.repo_key, rc.status, rc.last_seen_at, rc.last_ip, rc.spec, rc.org_id, "
           "rc.cpu_percent, rc.memory_percent, rc.disk_percent, rc.load_avg "
           "FROM runtime_credentials rc WHERE rc.id=?")
    with _any_cursor() as c:
        c.execute(sql, (int(runtime_id),))
        rows = _rows(c)
    return rows[0] if rows else None


# N5：只开放明确登记的 namespace（key 前缀）。meta 表里混有口令/webhook token，
# 绝不能按前缀通配直接返回——新增 namespace 必须在此登记并经评审。
_META_NAMESPACES = ("a2a-redo", "bot_token_health", "allow_signup", "enable_aligned",
                    "admin_initialized", "global_bot_usernames", "global_bot_token_status",
                    "skill_sync", "scene_sync")


def get_meta_namespace(namespace: str, limit: int = 200) -> dict[str, str]:
    """KV 元数据批量读（N5）：仅白名单 namespace，杜绝密钥外泄。"""
    ns = (namespace or "").strip()
    if ns not in _META_NAMESPACES:
        raise ValueError(f"namespace '{ns}' 未登记（拒绝读取）")
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT `key`, `value` FROM meta WHERE `key`=%s OR `key` LIKE %s "
                  "ORDER BY `key` LIMIT %s", (ns, ns + ":%", max(1, min(int(limit), 500))))
        rows = c.fetchall()
    return {r[0]: r[1] for r in rows}


def tasks_brief_many(task_ids: list[int]) -> list[dict[str, Any]]:
    """批量取任务摘要（N11）：列表页一屏一次调用，禁止 N+1。"""
    ids = [int(t) for t in (task_ids or []) if str(t).strip().isdigit() or isinstance(t, int)]
    if not ids:
        return []
    ids = ids[:200]
    marks = ",".join(["%s"] * len(ids))
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT id, repo_key, agent_type, skill, status, exit_code, "
                  f"runtime_id, event_seq, started_at, completed_at, created_at "
                  f"FROM agent_tasks WHERE id IN ({marks})", tuple(ids))
        rows = _rows(c)
    order = {tid: i for i, tid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(int(r["id"]), 10**9) if r.get("id") is not None else 10**9)
    return rows


def task_stats_extended(repo_key: str = "", since: str | None = None) -> dict[str, Any]:
    """任务聚合统计（N12）：替代洞察侧宽 SQL（任务量/完成率/时长）。"""
    base = task_stats(repo_key, since)
    with db_registry.cursor("runtime") as c:
        sql = ("SELECT COUNT(*) AS total, "
               "SUM(CASE WHEN status IN ('completed','success') THEN 1 ELSE 0 END) AS done, "
               "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
               "AVG(CASE WHEN started_at IS NOT NULL AND completed_at IS NOT NULL "
               "     THEN TIMESTAMPDIFF(SECOND, started_at, completed_at) END) AS avg_duration_s "
               "FROM agent_tasks WHERE 1=1")
        args: list[Any] = []
        if repo_key:
            sql += " AND repo_key=%s"
            args.append(repo_key)
        if since:
            sql += " AND created_at>=%s"
            args.append(since)
        c.execute(sql, tuple(args))
        row = c.fetchone()
        cols = [d[0] for d in (c.description or [])]
    return {"by_status": base, "aggregate": dict(zip(cols, row)) if row else {}}


def health() -> dict[str, Any]:
    try:
        with db_registry.cursor("runtime") as c:
            c.execute("SELECT COUNT(*) FROM agent_runtimes")
            n = c.fetchone()[0]
        return {"ok": True, "runtimes": n, "dsn_configured": bool(db_registry.dsn_of("runtime"))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "dsn_configured": bool(db_registry.dsn_of("runtime"))}


__all__ = ['ensure_issue_card', 'update_issue_card_fields', 'ensure_agent_session', 'update_agent_session_fields', 'delete_agent_sessions', 'update_issue_agent_session_fields', "available", "list_runtimes", "list_runtimes_rows", "get_runtime",
           "task_stats", "append_task_message", "aggregate_counts", "get_skill_by_id",
           "get_skill_content_hash", "count_skills", "latest_task_for_event",
           "task_brief", "list_credential_rows", "list_repo_credentials", "has_engine_capability", "list_engine_capabilities",
           "list_engine_tags", "query_events", "count_events", "group_events", "get_autopilot_execution", "upsert_engine_capability",
           "delete_engine_capability", "update_engine_capability", "renew_channel_lease", "acquire_channel_lease", "release_channel_lease",
           "release_all_leases", "heartbeat_instance", "list_active_instances", "health",
           # P3-5g 写原语（mgmt / insight 进程写 runtime 基表）
           "purge_user_rows", "delete_chat_conversations", "delete_vod_conversations",
           "detach_agent_sessions", "clear_agent_session_runtime", "ensure_agent_session",
           "stamp_task_traces",
           # P3-5h 写原语
           "mark_detach_rebound", "delete_autopilot_executions", "set_autopilot_findings",
           "set_repo_im_notifications", "add_event_action_filter",
           "update_event_action_filter", "delete_event_action_filter",
           # P3-5f 写原语（skill / 凭据）
           "update_skill_content", "update_skill_obs", "insert_skill", "create_skill_row",
           "delete_skill_row", "delete_skill_by_id", "adjust_skill_like_count",
           "insert_runtime_credential", "rename_runtime_credential",
           "set_runtime_credential_scope", "delete_runtime_credential",
           "upsert_repo_credential", "clear_repo_credential",
           "ensure_device_credential", "touch_credential_metrics",
           "upsert_local_runtimes", "ensure_default_runtime", "set_agent_tags",
           "purge_runtime_credential", "change_credential_scope", "reassign_repo_org_id",
           # P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12）
           "list_skills", "get_skill_detail", "list_all_engine_capabilities",
           "get_runtime_credential_meta", "get_meta_namespace", "tasks_brief_many",
           "task_stats_extended",
           "read_one", "read_all",
           # P4 收口：任意拓扑可用原语（单库/sqlite 与独立 DSN 均可用；可移植方言）
           "read_one_any", "read_all_any", "exec_any", "get_meta_value_any",
           "set_meta_value_any", "set_meta_if_absent", "set_meta_values_any",
           "merge_meta_json_any", "list_featured_skills_any",
           # P4 洞察看板聚合原语（insight 只读分析域唯一取数口；任意拓扑）
           "query_events_any", "event_dashboard_metrics",
           "dashboard_task_status_counts", "dashboard_issue_status_counts",
           "dashboard_stuck_tasks", "dashboard_failed_tasks", "dashboard_task_by_source",
           "dashboard_today_status_counts", "dashboard_running_counts",
           "dashboard_avg_durations", "dashboard_workers", "dashboard_local_runtimes",
           "dashboard_runtimes_with_credential", "credential_brief", "credential_scope_owner",
           "task_event_map", "runtime_device_map", "agent_sessions_with_runtime",
           "efficiency_issue_contrib", "task_issue_attribution",
           # IM 巡检执行卡（runtime/im/handler_async.py；任意拓扑）
           "autopilot_execution_detail_any",
           # 指标桶取数（insight/observability/metric_rollup；任意拓扑）
           "task_status_counts_by_created_any", "task_status_counts_by_completed_any",
           "task_timeout_counts_by_repo_any", "task_timing_rows_any",
           "event_cascade_counts_by_repo_any", "online_credential_count_any"]


# ---------- N13：跨域写补偿应用端点（幂等分发） ----------

def xwrite_apply(action: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """执行一条来自其它域的跨域写意图（N13）。

    约束（方案 §3.5 M1/M3/M6）：
    - action 必须在 `_XWRITE_ACTIONS` 登记（不接受调用方传 SQL/表名）；
    - `idempotency_key` 必填，目标域先登记（`cross_write.claim_applied`）再执行；
    - 未登记 action → ValueError（HTTP 400），不静默。
    """
    from src.kernel import cross_write as xw

    handlers = {
        "engine_cap_upsert": lambda p: upsert_engine_capability(
            int(p["rid"]), str(p["tag"]), priority=int(p.get("priority", 50))),
        "engine_cap_update": lambda p: update_engine_capability(
            int(p["rid"]), str(p["tag"]), int(p.get("priority", 50)), int(p.get("enabled", 1))),
        "engine_cap_delete": lambda p: delete_engine_capability(
            int(p["rid"]), p.get("tag")),
        "engine_cap_delete_all": lambda p: delete_engine_capability(int(p["rid"])),
        "task_message_append": lambda p: append_task_message(
            str(p["task_id"]), str(p.get("actor", "system")), str(p.get("content", "")),
            msg_type=str(p.get("type", "message"))),
        "agent_session_ensure": lambda p: {"created": ensure_agent_session(
            str(p["session_id"]), str(p["repo_key"]), str(p["agent_type"]),
            str(p.get("status", "idle")))},
        "agent_session_clear": lambda p: {"cleared": clear_agent_session_runtime(
            str(p["repo_key"]), str(p["agent_type"]))},
        "repo_credential_upsert": lambda p: upsert_repo_credential(
            str(p["repo_key"]), str(p["cred_type"]), str(p["value"]),
            str(p.get("updated_at", ""))),
        "repo_credential_clear": lambda p: {"cleared": clear_repo_credential(
            str(p["repo_key"]), str(p["cred_type"]), str(p.get("updated_at", "")))},
    }
    fn = handlers.get(action)
    if fn is None:
        raise ValueError(f"unregistered xwrite action: {action!r}")
    if not xw.claim_applied(idempotency_key, action):
        return {"status": "duplicate", "action": action, "idempotency_key": idempotency_key}
    result = fn(payload or {})
    return {"status": "applied", "action": action, "result": result}


# ---------- W2 写原语（api/mgmt 进程写 runtime 归属表；方案 §3.5 M1–M6） ----------

def upsert_event_active_guard(repo_key: str, issue_id: str, agent_type: str, seq: int,
                              created_at: str = "") -> int:
    """活动守卫状态 upsert（W2）：主键 (repo_key, issue_id, agent_type)，值取最新 seq。"""
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT INTO event_active_guard (repo_key, issue_id, agent_type, seq, created_at) "
                  "VALUES (%s, %s, %s, %s, COALESCE(NULLIF(%s, ''), NOW())) "
                  "ON DUPLICATE KEY UPDATE seq=VALUES(seq)",
                  (repo_key, str(issue_id), agent_type, int(seq), created_at or ""))
        return int(c.rowcount or 0)


def upsert_issue_agent_session(session_id: str, repo_key: str, agent_type: str,
                               issue_id: str, runtime_session_id: str = "",
                               session_engine: str = "", status: str = "active") -> dict[str, Any]:
    """Issue↔Agent 会话绑定 upsert（W2）：唯一键 (repo_key, agent_type, issue_id)。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE issue_agent_session SET runtime_session_id=%s, session_engine=%s, "
                  "status=%s, last_active_at=NOW() "
                  "WHERE repo_key=%s AND agent_type=%s AND issue_id=%s",
                  (runtime_session_id, session_engine, status, repo_key, agent_type,
                   str(issue_id)))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated", "id": session_id}
        try:
            c.execute("INSERT INTO issue_agent_session "
                      "(id, repo_key, agent_type, issue_id, runtime_session_id, session_engine, "
                      "status, last_active_at, created_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
                      (session_id, repo_key, agent_type, str(issue_id), runtime_session_id,
                       session_engine, status))
            return {"action": "inserted", "id": session_id}
        except Exception as exc:  # noqa: BLE001 - 并发撞唯一键：回退为更新
            log.info(f"issue agent session upsert raced ({session_id}): {exc}")
            c.execute("UPDATE issue_agent_session SET runtime_session_id=%s, session_engine=%s, "
                      "status=%s, last_active_at=NOW() "
                      "WHERE repo_key=%s AND agent_type=%s AND issue_id=%s",
                      (runtime_session_id, session_engine, status, repo_key, agent_type,
                       str(issue_id)))
            return {"action": "updated_after_conflict", "id": session_id}


def upsert_scene_category(name: str, slug: str, icon: str = "", order_num: int = 0,
                          is_active: int = 1, updated_by: str = "") -> dict[str, Any]:
    """场景分类 upsert（W2）：唯一键 slug；重放不新增行。"""
    from src.kernel.id_gen import uuid7_str

    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE scene_category SET name=%s, icon=%s, order_num=%s, is_active=%s, "
                  "updated_by=%s, updated_at=NOW() WHERE slug=%s",
                  (name, icon, int(order_num), int(is_active), updated_by, slug))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated", "slug": slug}
        try:
            new_id = uuid7_str()
            c.execute("INSERT INTO scene_category "
                      "(id, name, slug, icon, order_num, is_active, created_by, updated_by, "
                      "created_at, updated_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
                      (new_id, name, slug, icon, int(order_num), int(is_active), updated_by,
                       updated_by))
            return {"action": "inserted", "id": new_id}
        except Exception as exc:  # noqa: BLE001
            log.info(f"scene category upsert raced ({slug}): {exc}")
            c.execute("UPDATE scene_category SET name=%s, icon=%s, order_num=%s, is_active=%s, "
                      "updated_by=%s, updated_at=NOW() WHERE slug=%s",
                      (name, icon, int(order_num), int(is_active), updated_by, slug))
            return {"action": "updated_after_conflict", "slug": slug}


def record_event_skipped(repo_key: str, issue_id: str, event_type: str, reason: str,
                         detail: str = "") -> int:
    """记录被跳过的事件（W3 派生流水），返回新行 id。"""
    with db_registry.cursor("runtime") as c:
        c.execute("INSERT INTO event_skipped (repo_key, issue_id, event_type, reason, detail, "
                  "created_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                  (repo_key, str(issue_id), event_type, reason, detail))
        return int(getattr(c, "lastrowid", 0) or 0)


# ---------- P3.5：mgmt 进程读运行域会话/分类（读路径接口化） ----------

_CONV_COLS = ("id", "user_id", "agent_session_id", "runtime_session_id", "user_gitcode_token",
              "repo_key", "im_channel_id", "title", "status", "created_at", "updated_at",
              "conv_type", "issue_number")


def list_user_conversations(user_id: int, repo_key: str = "", limit: int = 20) -> list[dict]:
    """按用户列会话（替代 mgmt/kernel 直读 chat_conversations），列与语义同 kernel 版。"""
    sql = f"SELECT {', '.join(_CONV_COLS)} FROM chat_conversations WHERE user_id=%s"
    args: list[Any] = [user_id]
    if repo_key:
        sql += " AND repo_key=%s"
        args.append(repo_key)
    sql += " ORDER BY updated_at DESC LIMIT %s"
    args.append(max(1, min(int(limit), 200)))
    with db_registry.cursor("runtime") as c:
        c.execute(sql, tuple(args))
        rows = _rows(c)
    return [{
        "id": r["id"], "user_id": r["user_id"], "agent_session_id": r["agent_session_id"] or "",
        "runtime_session_id": r["runtime_session_id"] or "",
        "user_gitcode_token": r["user_gitcode_token"] or "", "repo_key": r["repo_key"] or "",
        "im_channel_id": r["im_channel_id"], "title": r["title"], "status": r["status"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "conv_type": r["conv_type"] or "general", "issue_number": r["issue_number"] or 0,
    } for r in rows]


def get_scene_category(category_id: int) -> dict[str, Any] | None:
    """场景分类单条（替代 mgmt 直读 scene_category）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, name, slug, icon, order_num, is_active FROM scene_category "
                  "WHERE id=%s", (int(category_id),))
        rows = _rows(c)
    return rows[0] if rows else None


def list_scene_categories(active_only: bool = True) -> list[dict[str, Any]]:
    """场景分类列表（market/scene 列表页用）。"""
    sql = "SELECT id, name, slug, icon, order_num, is_active FROM scene_category"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY order_num, id"
    with db_registry.cursor("runtime") as c:
        c.execute(sql)
        return _rows(c)


def list_featured_skills(ordered_ids: list[int] | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """精选技能（is_featured=1 的 official 技能），排序与 mgmt 版一致。

    ordered_ids 非空时按其顺序（管理台排序），缺失的按 like_count 降序追加；
    为空时直接 like_count 降序。列与 mgmt `get_featured_skills` 完全一致。
    """
    cols = ("id", "skill_name", "display_name", "description", "agent_type",
            "is_featured", "like_count", "category_id")
    sel = f"SELECT {', '.join(cols)} FROM skill WHERE is_active=1 AND is_featured=1 AND scope='official'"
    out: list[dict[str, Any]] = []
    with db_registry.cursor("runtime") as c:
        if ordered_ids:
            ph = ",".join(["%s"] * len(ordered_ids))
            c.execute(f"{sel} AND id IN ({ph})", tuple(ordered_ids))
            rows = {r["id"]: r for r in _rows(c)}
            for item_id in ordered_ids:
                r = rows.get(int(item_id))
                if r:
                    out.append(_skill_row(r))
            if len(out) < limit:
                c.execute(f"{sel} AND id NOT IN ({ph}) ORDER BY like_count DESC", tuple(ordered_ids))
                out.extend(_skill_row(r) for r in _rows(c))
        else:
            c.execute(f"{sel} ORDER BY like_count DESC LIMIT %s", (max(1, int(limit)),))
            out = [_skill_row(r) for r in _rows(c)]
    return out[:max(1, int(limit))]


def _skill_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "skill_name": r["skill_name"],
            "display_name": r["display_name"] or r["skill_name"],
            "description": r["description"] or "", "agent_type": r["agent_type"] or "",
            "is_featured": r["is_featured"], "like_count": r["like_count"],
            "category_id": r["category_id"] or 0}


def list_active_credential_ids() -> list[int]:
    """active 凭据 id 列表（settings 设备/引擎列表的循环源，替代跨域裸 SQL）。"""
    with _any_cursor() as c:
        c.execute("SELECT id FROM runtime_credentials WHERE status='active' ORDER BY id DESC")
        return [r["id"] for r in _rows(c)]


def list_runtime_credentials(org_id: str = "", personal_org: bool = True) -> list[dict[str, Any]]:
    """凭据列表（不含 token_hash/明文）。非个人组织严格按 org_id 过滤，个人组织返回全部。

    返回列与 settings `list_runtime_credentials` 一致；repo_key 由调用方按需转显示值。
    """
    sql = ("SELECT id, name, device_name, scope, owner_id, owner_name, status, "
           "last_seen_at, created_at, repo_key FROM runtime_credentials")
    args: list[Any] = []
    if org_id and not personal_org:
        sql += " WHERE org_id=?"
        args.append(org_id)
    sql += " ORDER BY id DESC"
    with _any_cursor() as c:
        c.execute(sql, tuple(args))
        rows = _rows(c)
    return [{"id": r["id"], "name": r["name"], "device_name": r["device_name"] or "",
             "scope": r["scope"], "owner_id": r["owner_id"], "owner": r["owner_name"] or "",
             "status": r["status"], "last_seen_at": r["last_seen_at"],
             "created_at": r["created_at"], "repo_key": r["repo_key"] or ""}
            for r in rows]


_AUTOPILOT_EXEC_PAGE_COLS = (
    "e.id, e.task_id, e.status, e.retry_count, e.result, e.error, e.started_at, "
    "e.completed_at, e.created_at, e.agent_type, e.findings_json")
# 迁移兼容列白名单：v3.0 前的老库物理保留 / 新列未落时以空串占位（1054 → 500 的既有修复）。
# 列表达式只允许从本表挑，调用方不得传入列名；字典序即 SELECT 列序。
_AUTOPILOT_EXEC_COMPAT_COLS: dict[str, tuple[str, str]] = {
    "issue_number": ("e.issue_number", "'' AS issue_number"),
    "agent_task_id": ("e.agent_task_id", "'' AS agent_task_id"),
}
# 过滤谓词只允许从本表挑（值一律走 `?` 绑定，不拼值）
_AUTOPILOT_EXEC_FILTERS: dict[str, str] = {
    "task": "e.task_id=?",
    "status": "e.status=?",
}


def list_autopilot_executions(task_id: int | None = None, status: str = "",
                              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """autopilot 执行记录分页读（**任意拓扑**；C2 表归运行域，mgmt 两条拓扑共用）。

    列口径与原 mgmt `list_executions` 兜底 SQL 一致（13 列，同序）；issue_number /
    agent_task_id 在旧库可能不存在，用 `SELECT * ... LIMIT 0` 的游标描述符在**本域连接**
    上探测后以空串占位（原实现是在 mgmt 连接上探测运行域表，三库拓扑下恒探测为空）。
    为什么不用 PRAGMA table_info：db_registry 的薄兼容游标只做方言翻译、不做 PRAGMA
    特判（那是 mysql_compat.Cursor 才有），直发 PRAGMA 在 MySQL 上会 1064；
    描述符探测两条后端一致。
    可变部分只有过滤组合与兼容列，二者都由本模块白名单给出，不接收调用方 SQL/列名。
    """
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    conds, args = [], []
    if task_id is not None:
        conds.append(_AUTOPILOT_EXEC_FILTERS["task"])
        args.append(int(task_id))
    if status:
        conds.append(_AUTOPILOT_EXEC_FILTERS["status"])
        args.append(status)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    with _any_cursor() as c:
        c.execute("SELECT * FROM autopilot_executions LIMIT 0")
        cols = {str(d[0] or "") for d in (getattr(c, "description", None) or [])}
        compat = ", ".join(present if name in cols else missing
                           for name, (present, missing) in _AUTOPILOT_EXEC_COMPAT_COLS.items())
        c.execute(f"SELECT {_AUTOPILOT_EXEC_PAGE_COLS}, {compat} "
                  f"FROM autopilot_executions e WHERE 1=1{where} "
                  f"ORDER BY e.id DESC LIMIT ? OFFSET ?", tuple(args + [lim, off]))
        rows = c.fetchall()
        c.execute(f"SELECT COUNT(*) FROM autopilot_executions e WHERE 1=1{where}", tuple(args))
        total_row = c.fetchone()
    keys = ("id", "task_id", "status", "retry_count", "result", "error", "started_at",
            "completed_at", "created_at", "agent_type", "findings_json", "issue_number",
            "agent_task_id")
    return {"executions": [dict(zip(keys, tuple(r))) for r in rows],
            "total": int(total_row[0]) if total_row else 0,
            "limit": lim, "offset": off}


# ---------- W2：meta 配置（基表在运行域，mgmt 经 lib 读写） ----------

def get_meta(key: str) -> str:
    """读 meta 配置值（不存在返回空串）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT value FROM meta WHERE `key`=%s", (key,))
        rows = _rows(c)
    return (rows[0]["value"] or "") if rows else ""


def set_meta_value(key: str, value: str) -> None:
    """写 meta 配置值（upsert，幂等）。"""
    # UPDATE→INSERT 两段式（不用 ON DUPLICATE KEY：本项目连接层会改写该子句导致 1064；
    # 两段式也天然方言无关，sqlite 单库期同样可用）
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE meta SET value=%s WHERE `key`=%s", (value, key))
        if not getattr(c, "rowcount", 0):
            try:
                c.execute("INSERT INTO meta (`key`, value) VALUES (%s, %s)", (key, value))
            except Exception:
                # 并发插入撞唯一键：再更新一次即可（幂等）
                c.execute("UPDATE meta SET value=%s WHERE `key`=%s", (value, key))


# ---------- P4 收口：任意拓扑可用的 runtime 基表原语 ----------
#
# 为什么需要这一组：mgmt 侧跨域读/写此前是「lib 优先 + 本地 SQL 兜底」，兜底 SQL 的
# 表名留在外域文件里 → 静态面恒计一处跨域引用（三库独立化棘轮的剩余项）。本组把同一段
# SQL 搬进 owner 模块，连接按拓扑自适应：
#   - 配了自有 DSN（三库三机）→ `db_registry` 连接（MySQL 方言由 translate_sql 翻译）；
#   - 未配（单库 / sqlite / 测试拓扑）→ `kernel.domain_db` 回落进程主库。
# 与既有 `read_one` / `read_all` 白名单的区别：后者是 MySQL 原生方言（`%s` + `NOW()`），
# 只在自有 DSN 下可用；本组 SQL 一律**可移植方言**（`?` 占位符 / `INSERT OR IGNORE`），
# 两条连接都能执行，因此调用方不再需要在外域文件里留一份本地 SQL 兜底。
# 约束：只登记固定 SQL 的具名查询（参数化），不接受调用方传入 SQL 文本或表名。

# 会话/消息列清单（具名查询共用；列序即调用方取值的下标，改动必须同步所有消费者）
_CONV_COLS_SQL = ", ".join(_CONV_COLS)
_MSG_COLS = "id, conversation_id, role, content, created_at, task_id, generate_status, deleted"

_ANY_QUERIES: dict[str, str] = {
    # ── session_manager 去跨域所需具名查询（kernel 侧会话/消息/群绑定/配置行）──
    # 列序与原 SQL 逐字一致：调用方按下标取值，错位即静默串列，因此只允许**照抄**。
    'chat_session_by_repo': 'SELECT id, event_history, runtime_session_id, status FROM chat_agent_session WHERE repo_key=?',
    'chat_session_insert': (
        "INSERT INTO chat_agent_session (id, repo_key, source, repository, status, event_history, "
        "created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)"),
    'chat_session_history_reset': 'UPDATE chat_agent_session SET event_history=? WHERE id=?',
    'chat_session_set_status': 'UPDATE chat_agent_session SET status=? WHERE id=?',
    'chat_session_update_state': (
        "UPDATE chat_agent_session SET event_history=?, status=?, runtime_session_id=?, "
        "last_active_at=? WHERE id=?"),
    'chat_session_update_state_noreid': (
        "UPDATE chat_agent_session SET event_history=?, status=?, last_active_at=? WHERE id=?"),
    'chat_config_row_canonical': (
        "SELECT id, runtime_id, skill, skill_id FROM chat_agent_session "
        "WHERE id=? AND user_id=? AND (repo_key='' OR repo_key IS NULL)"),
    'chat_config_rows_by_user': (
        "SELECT id, runtime_id, skill, skill_id FROM chat_agent_session "
        "WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) "
        "ORDER BY last_active_at DESC, id DESC"),
    'chat_config_delete_others': (
        "DELETE FROM chat_agent_session WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) AND id!=?"),
    'chat_config_upsert': (
        "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, skill, "
        "skill_id, created_at, last_active_at) "
        "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))"),
    # chat_conversations
    'conversation_insert': (
        "INSERT INTO chat_conversations (id, user_id, agent_session_id, runtime_session_id, "
        "repo_key, im_channel_id, title, user_gitcode_token, conv_type, issue_number) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)"),
    'conversations_by_user_repo': (
        f"SELECT {_CONV_COLS_SQL} FROM chat_conversations WHERE user_id=? AND repo_key=? "
        "ORDER BY updated_at DESC LIMIT ?"),
    'conversations_by_user': (
        f"SELECT {_CONV_COLS_SQL} FROM chat_conversations WHERE user_id=? "
        "ORDER BY updated_at DESC LIMIT ?"),
    'conversation_by_id': f"SELECT {_CONV_COLS_SQL} FROM chat_conversations WHERE id=?",
    'conversation_by_im_user': (
        f"SELECT {_CONV_COLS_SQL} FROM chat_conversations WHERE im_channel_id=? AND user_id=? "
        "ORDER BY updated_at DESC LIMIT 1"),
    'conversation_by_im': (
        f"SELECT {_CONV_COLS_SQL} FROM chat_conversations WHERE im_channel_id=? "
        "ORDER BY updated_at DESC LIMIT 1"),
    'conversation_brief': (
        "SELECT user_id, repo_key, conv_type, issue_number FROM chat_conversations WHERE id=?"),
    'conversation_owner': 'SELECT user_id FROM chat_conversations WHERE id=?',
    'conversation_touch': "UPDATE chat_conversations SET updated_at=datetime('now') WHERE id=?",
    'conversation_clear_im_channel': (
        "UPDATE chat_conversations SET im_channel_id='' WHERE im_channel_id=? AND id!=?"),
    'conversation_set_im_channel': "UPDATE chat_conversations SET im_channel_id=? WHERE id=?",
    'conversation_delete': 'DELETE FROM chat_conversations WHERE id=?',
    # chat_messages
    'message_insert_with_task': (
        "INSERT INTO chat_messages (conversation_id, role, content, task_id, generate_status) "
        "VALUES (?,?,?,?,?)"),
    'message_insert': (
        "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)"),
    'message_by_id': f"SELECT {_MSG_COLS} FROM chat_messages WHERE id=?",
    'message_recall': 'UPDATE chat_messages SET deleted=1 WHERE id=? AND deleted=0',
    'message_recall_by_task': (
        "UPDATE chat_messages SET deleted=1 WHERE task_id=? "
        "AND role IN ('agent','assistant') AND deleted=0"),
    'message_user_deleted': (
        "SELECT deleted FROM chat_messages WHERE task_id=? AND role='user' "
        "ORDER BY id DESC LIMIT 1"),
    'messages_page_before': (
        f"SELECT {_MSG_COLS} FROM chat_messages WHERE conversation_id=? AND id<? "
        "ORDER BY id DESC LIMIT ?"),
    'messages_page': (
        f"SELECT {_MSG_COLS} FROM chat_messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT ?"),
    'messages_delete_by_conversation': 'DELETE FROM chat_messages WHERE conversation_id=?',
    # repo_im_group_binding
    'repo_im_flags_latest': (
        'SELECT auto_impl_enabled, notify_enabled FROM repo_im_group_binding '
        'ORDER BY id DESC LIMIT 1'),
    'repo_im_set_notifications': (
        'UPDATE repo_im_group_binding SET auto_impl_enabled=?, notify_enabled=?'),
    'repo_im_auto_impl': 'SELECT auto_impl_enabled FROM repo_im_group_binding WHERE repo_key=?',
    'repo_im_set_auto_impl': (
        'UPDATE repo_im_group_binding SET auto_impl_enabled=? WHERE repo_key=?'),
    'repo_im_notify': 'SELECT notify_enabled FROM repo_im_group_binding WHERE repo_key=?',
    'repo_im_set_notify': 'UPDATE repo_im_group_binding SET notify_enabled=? WHERE repo_key=?',
    'repo_im_binding_upsert': (
        "INSERT INTO repo_im_group_binding (im_provider, im_chat_id, repo_key, source, bind_by, "
        "im_app_id) VALUES ('feishu',?,?,?,?,?) "
        "ON CONFLICT(im_provider, im_chat_id, im_app_id) DO UPDATE SET "
        "repo_key=excluded.repo_key, source=excluded.source, bind_by=excluded.bind_by, "
        "im_app_id=excluded.im_app_id"),
    # issue：会话收到真实用户消息 → 任务转 processing
    'issue_processing_by_conversation': (
        "UPDATE issue SET status='processing' WHERE conversation_id=? AND user_id=? "
        "AND status='active'"),
    'issue_processing_by_number': (
        "UPDATE issue SET status='processing' WHERE repo_key=? AND issue_number=? AND user_id=? "
        "AND status='active'"),
    # ── session_store 去跨域所需具名查询（见 src/api/session_store.py 注释）──
    'agent_type_enable': 'SELECT enable FROM agent_type WHERE agent_type=?',
    'devops_agent_session_row': 'SELECT id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?',
    'devops_agent_sessions_by_repo': 'SELECT id, agent_type, runtime_id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? ORDER BY agent_type',
    'devops_agent_session_insert': "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token, status, enable, subscribe_events, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
    'devops_agent_session_delete_by_repo_agent': 'DELETE FROM devops_agent_session WHERE repo_key=? AND agent_type=?',
    'devops_agent_session_delete_by_repo': 'DELETE FROM devops_agent_session WHERE repo_key=?',
    'agent_runtime_name_by_id': 'SELECT name FROM agent_runtimes WHERE id=?',
    'issue_card_row': 'SELECT stage, owner_agent, a2a_seq, updated_at, assignee, scene_id, scene_user_id, parent_issue_id, is_subtask_mode, allow_scene_override, carried_context, status FROM issue WHERE repo_key=? AND issue_number=?',
    'issue_card_exists': 'SELECT id FROM issue WHERE repo_key=? AND issue_number=?',
    'autopilot_execution_brief': 'SELECT id, task_id FROM autopilot_executions WHERE id=?',
    'autopilot_execution_findings': (
        'SELECT task_id, findings_json FROM autopilot_executions WHERE id=?'),
    'issue_card_ensure_insert_plain': (
        "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_by) "
        "VALUES (?,?,?,?,'active',?)"),
    'issue_state_legacy_row': 'SELECT id, stage, owner_agent, a2a_seq, updated_at, assignee, scene_id, scene_user_id, parent_issue_id, is_subtask_mode, allow_scene_override FROM issue_state WHERE repo_key=? AND issue_id=?',
    'issue_children_by_parent': 'SELECT issue_number, stage, owner_agent FROM issue WHERE repo_key=? AND parent_issue_id=? ORDER BY issue_number',
    'issue_release_children': "SELECT id, issue_number, title, status, stage, release_candidate_sha, release_target_env, release_evidence_ref, release_result, release_failure_reason, release_approved_by, release_approved_at, created_at, completed_at FROM issue WHERE repo_key=? AND parent_issue_number=? AND release_target_env != '' ORDER BY id DESC",
    'issue_status_sync': "UPDATE issue SET status=? WHERE repo_key=? AND issue_number=? AND status NOT IN ('completed','cancelled','awaiting_input','pending_review')",
    'issue_agent_session_row': 'SELECT id, runtime_session_id, status, last_active_at, session_engine FROM issue_agent_session WHERE repo_key=? AND agent_type=? AND issue_id=?',
    'issue_agent_session_insert': 'INSERT INTO issue_agent_session (id, repo_key, agent_type, issue_id, runtime_session_id, session_engine, status, last_active_at) VALUES (?,?,?,?,?,?,?,?)',
    'issue_agent_sessions_by_repo': 'SELECT id, repo_key, agent_type, issue_id, runtime_session_id, status, last_active_at, created_at FROM issue_agent_session WHERE repo_key=? ORDER BY last_active_at DESC',
    'issue_agent_sessions_by_repo_agent': 'SELECT id, repo_key, agent_type, issue_id, runtime_session_id, status, last_active_at, created_at FROM issue_agent_session WHERE repo_key=? AND agent_type=? ORDER BY last_active_at DESC',
    'issue_agent_session_delete_by_repo': 'DELETE FROM issue_agent_session WHERE repo_key=?',
    # meta 配置（基表在运行域）
    "meta_value": "SELECT value FROM meta WHERE key=?",
    "meta_count": "SELECT COUNT(*) FROM meta WHERE key=?",
    # devops_agent_session：仓库 session token 判存（mgmt/user/db 路径 A 判据）
    "devops_agent_session_repo_token": (
        "SELECT repo_gitcode_token FROM devops_agent_session "
        "WHERE repo_key=? AND repo_gitcode_token IS NOT NULL AND repo_gitcode_token!='' LIMIT 1"),
    # scene_category（mgmt/user/db 的场景分类读路径）
    "scene_categories_active": (
        "SELECT id, name, slug, icon, order_num FROM scene_category "
        "WHERE is_active=1 ORDER BY order_num"),
    "scene_category_row": "SELECT id, name, slug, icon FROM scene_category WHERE id=?",
    # runtime_credentials / agent_runtimes / engine_capabilities（settings 引擎管理台读路径）
    "runtime_credentials_all": (
        "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, "
        "last_ip, repo_key, org_id, spec FROM runtime_credentials"),
    "runtime_credentials_active_ids": (
        "SELECT id FROM runtime_credentials WHERE status='active' ORDER BY id DESC"),
    "runtime_credentials_list": (
        "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, "
        "created_at, repo_key FROM runtime_credentials ORDER BY id DESC"),
    "runtime_credentials_by_org": (
        "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, "
        "created_at, repo_key FROM runtime_credentials WHERE org_id=? ORDER BY id DESC"),
    "runtime_credential_exists": "SELECT id FROM runtime_credentials WHERE id=?",
    "runtime_credential_owner_status": (
        "SELECT owner_name, status FROM runtime_credentials WHERE id=?"),
    "runtime_credential_owner_name": "SELECT owner_name FROM runtime_credentials WHERE id=?",
    "runtimes_by_credential_full": (
        "SELECT id, name, version, healthy, installed, status, last_seen_at, local_skills, "
        "skill_scan_result, worker_version FROM agent_runtimes WHERE credential_id=?"),
    "runtime_exists_any": "SELECT 1 FROM agent_runtimes WHERE id=?",
    # 引擎实例解析（engine/instances.py：三库独立后同域代码也必须走 owner 连接）
    "runtimes_for_instance_scan": (
        "SELECT ar.id, ar.name, ar.credential_id, rc.device_name, rc.status "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "ORDER BY CASE WHEN ar.credential_id = 0 OR rc.status IS NULL OR rc.status = 'active' "
        "THEN 0 ELSE 1 END, ar.id"),
    "runtimes_for_instance_scan_scope": (
        "SELECT ar.id, ar.name, ar.credential_id, rc.device_name, rc.status "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "WHERE ar.scope = ? "
        "ORDER BY CASE WHEN ar.credential_id = 0 OR rc.status IS NULL OR rc.status = 'active' "
        "THEN 0 ELSE 1 END, ar.id"),
    "runtime_credential_id": "SELECT credential_id FROM agent_runtimes WHERE id=?",
    "runtime_credential_scope": "SELECT scope FROM runtime_credentials WHERE id=?",
    "runtime_credentials_scope_owner": (
        "SELECT scope, owner_name FROM runtime_credentials WHERE id=?"),
    "runtime_credentials_name_device": (
        "SELECT name, device_name FROM runtime_credentials WHERE id=?"),
    # 技能页（mgmt/routes/skills.py）读路径
    "skill_exists_active": "SELECT 1 FROM skill WHERE id=? AND is_active=1",
    "skill_official_exact": (
        "SELECT id, skill_name, display_name, description, agent_type, category_id FROM skill "
        "WHERE is_active=1 AND scope='official' AND (skill_name=? OR display_name=?) "
        "ORDER BY skill_name"),
    "skill_official_all": (
        "SELECT id, skill_name, display_name, description, agent_type, category_id FROM skill "
        "WHERE is_active=1 AND scope='official' ORDER BY agent_type, skill_name"),
    "skill_detail_by_id": (
        "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id, "
        "is_featured, like_count, sub_scene, category_id FROM skill WHERE id=? AND is_active=1"),
    "skill_detail_by_name": (
        "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id, "
        "is_featured, like_count, sub_scene, category_id FROM skill WHERE skill_name=? AND is_active=1 "
        "AND (scope='official' OR (scope='user' AND owner_id=?) OR scope='repo') "
        "ORDER BY CASE scope WHEN 'user' THEN 0 WHEN 'repo' THEN 1 ELSE 2 END, id DESC LIMIT 1"),
    "skill_path_owner_scope": (
        "SELECT skill_name, owner_id, skill_path, scope FROM skill WHERE id=? AND is_active=1"),
    "skill_meta_row": (
        "SELECT skill_name, scope, owner_id, agent_type, category_id FROM skill "
        "WHERE id=? AND is_active=1"),
    "skill_name_path": "SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1",
    "skill_repo_full_row": (
        "SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id "
        "FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 LIMIT 1"),
    "skill_name_path_scope": (
        "SELECT skill_name, skill_path, scope FROM skill WHERE id=? AND is_active=1"),
    # 会话配置页（mgmt/routes/agents.py）读路径
    "devops_session_vod_id": (
        "SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'"),
    "agent_type_all": (
        "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, "
        "enable FROM agent_type ORDER BY order_num, agent_type"),
    "agent_type_row": (
        "SELECT agent_type, display_name, description, default_runtime_id, icon, order_num, "
        "enable FROM agent_type WHERE agent_type=?"),
    "runtime_name_cred": "SELECT name, credential_id FROM agent_runtimes WHERE id=?",
    "credential_device_name": "SELECT device_name FROM runtime_credentials WHERE id=?",
    "credential_name": "SELECT name FROM runtime_credentials WHERE id=?",
    "runtime_host_name": "SELECT host_name FROM agent_runtimes WHERE id=?",
    "skill_scope_by_id": "SELECT scope FROM skill WHERE id=? AND is_active=1",
    "skill_id_by_name_scope": (
        "SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1"),
    "vod_conv_rows_by_session": (
        "SELECT vc.im_chat_id, vc.im_channel_name, vc.runtime_session_id, vc.status, "
        "vc.last_active_at FROM vod_conversations vc WHERE vc.agent_session_id=? "
        "ORDER BY vc.last_active_at DESC"),
    "devops_session_reset_runtime_session": (
        "UPDATE devops_agent_session SET runtime_session_id='' WHERE repo_key=? AND agent_type=?"),
    # ── mgmt 反馈路由（routes/feedback.py）读路径收口 ──────────────────────────
    # 这些站点此前是「lib 有 DSN 才走 lib，否则本地兜底 SQL」，而兜底 SQL 才是
    # 跨域统计的来源。改成任意拓扑具名查询后，SQL 只在 runtime 侧留一份。
    # 列序与原 SQL 逐字一致（调用方按下标取值）。
    "skill_resolve_preferred": (
        "SELECT id, skill_name, scope FROM skill WHERE skill_name=? AND is_active=1 "
        "ORDER BY CASE scope WHEN 'official' THEN 0 WHEN 'repo' THEN 1 ELSE 2 END LIMIT 1"),
    "event_feedback_row": (
        "SELECT seq, repo_key, issue_id, agent_type, status, runtime, result "
        "FROM events WHERE seq=?"),
    "issue_id_by_repo_number": (
        "SELECT id FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1"),
    "task_latest_by_event": (
        "SELECT id, status, runtime_id, exit_code, started_at, completed_at, "
        "result_text, skill FROM agent_tasks WHERE event_seq=? "
        "ORDER BY created_at DESC LIMIT 1"),
    "task_brief_row": (
        "SELECT event_seq, agent_type, skill, runtime_id, repo_key "
        "FROM agent_tasks WHERE id=? LIMIT 1"),
    "devops_session_runtime_id_by_repo_agent": (
        "SELECT runtime_id FROM devops_agent_session "
        "WHERE repo_key=? AND agent_type=? AND runtime_id>0 LIMIT 1"),
    # agent_runtimes 与 runtime_credentials 同属 runtime 域，JOIN 留在本域内
    "runtime_engine_attribution": (
        "SELECT ar.name, ar.credential_id, COALESCE(rc.device_name,''), "
        "COALESCE(rc.last_ip,''), COALESCE(ar.provider,'') "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc "
        "ON rc.id=ar.credential_id WHERE ar.id=?"),
    # 巡检（api/patrol.py）读路径：issue / events / agent_tasks / agent_runtimes
    "issue_status_by_repo_number": (
        "SELECT status FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1"),
    "issue_stage_by_repo_number": (
        "SELECT stage FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1"),
    "task_event_seq": "SELECT event_seq FROM agent_tasks WHERE id=? LIMIT 1",
    # 补偿终检读原任务终态（api/patrol_compensate.py RecheckHandler）：列序与原查询逐字一致
    "task_recheck_row": (
        "SELECT completed_at, agent_type, result_text FROM agent_tasks WHERE id=?"),
    "event_payload_status_a2a": (
        "SELECT payload, status FROM events WHERE repo_key=? AND issue_id=? "
        "AND event_type='a2a_event'"),
    "event_a2a_created_agent": (
        "SELECT created_at, agent_type FROM events WHERE repo_key=? AND issue_id=? "
        "AND event_type='a2a_event'"),
    "task_created_at_by_agent": (
        "SELECT created_at FROM agent_tasks WHERE repo_key=? AND issue_id=? AND agent_type=?"),
    "tasks_running_rows": (
        "SELECT id, repo_key, issue_id, agent_type, runtime_id, started_at, created_at, "
        "stream_ended FROM agent_tasks WHERE status='running'"),
    "tasks_queued_rows": (
        "SELECT id, repo_key, issue_id, agent_type, runtime_id, created_at "
        "FROM agent_tasks WHERE status='queued'"),
    "runtime_health_bits": (
        "SELECT credential_id, status, last_seen_at, installed, healthy "
        "FROM agent_runtimes WHERE id=?"),
    "tasks_completed_page_cutoff": (
        "SELECT id, repo_key, issue_id, agent_type, completed_at FROM agent_tasks "
        "WHERE status='completed' AND completed_at > ? AND id > ? ORDER BY id LIMIT ?"),
    "tasks_completed_page_all": (
        "SELECT id, repo_key, issue_id, agent_type, completed_at FROM agent_tasks "
        "WHERE status='completed' AND id > ? ORDER BY id LIMIT ?"),
    "runtime_instance_row": (
        "SELECT ar.name, rc.device_name, ar.credential_id "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "WHERE ar.id=?"),
    "engine_cap_flags": (
        "SELECT tag, priority, enabled FROM engine_capabilities "
        "WHERE runtime_id=? AND enabled=1 ORDER BY tag"),
    "engine_cap_tags_sorted": (
        "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag"),
    "engine_caps_by_runtime": (
        "SELECT id, tag, priority, enabled, created_at, updated_at FROM engine_capabilities "
        "WHERE runtime_id=? ORDER BY tag"),
    # agent_runtimes UTC 口径迁移（启动期，逐行归一存量超前行）
    "runtime_utc_caliber_rows": "SELECT id, last_seen_at, updated_at FROM agent_runtimes",
    "runtime_set_timestamps": (
        "UPDATE agent_runtimes SET last_seen_at=?, updated_at=? WHERE id=?"),
    # skill
    "skill_row_by_id": (
        "SELECT id, skill_name, display_name, description, agent_type, skill_path, file_path, "
        "scope, owner_id, content_hash FROM skill WHERE id=? AND is_active=1"),
    "skill_by_name_scope": (
        "SELECT id, content_hash, file_path FROM skill WHERE skill_name=? AND scope=? LIMIT 1"),
    "skill_hash_by_key": (
        "SELECT id, content_hash, file_path, is_active FROM skill "
        "WHERE skill_name=? AND scope=? AND owner_id=? LIMIT 1"),
    "skill_hash_by_id": "SELECT id, content_hash, file_path, is_active FROM skill WHERE id=?",
    "skill_hashes_by_name_scope": (
        "SELECT owner_id, content_hash FROM skill WHERE skill_name=? AND scope=? AND is_active=1"),
    "skill_id_user_owner": (
        "SELECT id FROM skill WHERE skill_name=? AND scope='user' AND owner_id=? AND is_active=1 "
        "LIMIT 1"),
    "skill_id_repo_owner": (
        "SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 "
        "LIMIT 1"),
    "skill_id_official": (
        "SELECT id FROM skill WHERE skill_name=? AND scope='official' AND is_active=1 LIMIT 1"),
    "skill_id_by_owner_scope": (
        "SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? LIMIT 1"),
    "skill_scope_repo_owner": (
        "SELECT id, owner_id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? "
        "AND is_active=1 LIMIT 1"),
    "skill_scope_official": (
        "SELECT id FROM skill WHERE skill_name=? AND scope='official' AND is_active=1 LIMIT 1"),
    "skill_scope_user_any": (
        "SELECT id, owner_id FROM skill WHERE skill_name=? AND scope='user' AND is_active=1 "
        "LIMIT 1"),
    "skills_official_by_agent": (
        "SELECT id, skill_name, display_name, description, agent_type, skill_path, file_path, "
        "scope, owner_id, content_hash FROM skill "
        "WHERE agent_type=? AND is_active=1 AND scope='official' ORDER BY id"),
    "skills_user_by_owner": (
        "SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id "
        "FROM skill WHERE scope='user' AND owner_id=? ORDER BY id"),
    "skill_like_count": "SELECT like_count FROM skill WHERE id=?",
    # skill 写（任意拓扑：mgmt 侧 skill 跨库引用收口的具名写查询）
    "skill_repo_owner_prefix": (
        "UPDATE skill SET owner_id='gitcode:'||owner_id "
        "WHERE scope='repo' AND owner_id!='' AND owner_id NOT LIKE '%:%'"),
    "skill_obs_update": (
        "UPDATE skill SET file_path=?, display_name=?, agent_type=?, description=?, "
        "category_id=?, updated_by=?, is_active=1, content_hash=?, updated_at=datetime('now') "
        "WHERE id=?"),
    "skill_obs_insert": (
        "INSERT INTO skill (skill_name, display_name, description, agent_type, file_path, "
        "scope, owner_id, created_by, updated_by, is_active, category_id, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)"),
    "skill_row_create_ignore": (
        "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, "
        "skill_path, scope, owner_id, created_by, updated_by) VALUES (?,?,?,?,?,?,?,?,?)"),
    "skill_like_inc": "UPDATE skill SET like_count=like_count+1 WHERE id=?",
    "skill_like_dec": "UPDATE skill SET like_count=like_count-1 WHERE id=?",
    # agent_runtimes
    "runtimes_all": (
        "SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, "
        "updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, "
        "skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, "
        "worker_version, org_id, spawn_fail_degraded_at, detached_from_scenes_at, "
        "probe_spawn_fail_count FROM agent_runtimes ORDER BY name"),
    "runtimes_by_scope": (
        "SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, "
        "updated_at, scope, owner_id, credential_id, status, last_seen_at, local_skills, "
        "skill_scan_result, agent_tags, host_name, max_concurrent, scene_tags, alias, "
        "worker_version, org_id, spawn_fail_degraded_at, detached_from_scenes_at, "
        "probe_spawn_fail_count FROM agent_runtimes WHERE scope=? AND (owner_id=? OR owner_id='') "
        "ORDER BY name"),
    "runtime_id_local_direct": (
        "SELECT id FROM agent_runtimes WHERE lower(name)=? AND credential_id=0 AND installed=1 "
        "ORDER BY id LIMIT 1"),
    "runtime_id_by_name_scope_owner": (
        "SELECT ar.id FROM agent_runtimes ar "
        "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "WHERE lower(ar.name)=? AND ar.scope=? AND ar.owner_id=? "
        "AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active') "
        "ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC LIMIT 1"),
    "runtime_id_by_name_scope_shared": (
        "SELECT ar.id FROM agent_runtimes ar "
        "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "WHERE lower(ar.name)=? AND ar.scope=? AND ar.owner_id='' "
        "AND (ar.credential_id=0 OR rc.status IS NULL OR rc.status='active') "
        "ORDER BY CASE WHEN ar.status='online' THEN 0 ELSE 1 END, ar.last_seen_at DESC LIMIT 1"),
    "runtime_age": "SELECT MIN(updated_at) FROM agent_runtimes",
    # skill 写
    "skill_update_content_hash": "UPDATE skill SET content_hash=?, file_path=? WHERE id=?",
    "skill_update_content_hash_only": "UPDATE skill SET content_hash=? WHERE id=?",
    "skill_delete_by_owner_scope": (
        "DELETE FROM skill WHERE skill_name=? AND scope=? AND owner_id=?"),
    # agent_runtimes 写
    "runtime_insert_default": (
        "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, "
        "pip_package, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,'')"),
    # meta 写
    "meta_upsert_update": "UPDATE meta SET value=? WHERE key=?",
    "meta_upsert_insert": "INSERT INTO meta (key, value) VALUES (?, ?)",
    "meta_insert_absent": "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
    # ── 引擎行 / 凭据单点读（kernel prompt_builder：本地性判定 + 引擎名注入）──
    # 原实现是两条单点查询（agent_runtimes → runtime_credentials），两表都属本域，
    # 合并成一次 LEFT JOIN：行不存在 → 无行（= 旧口径「查不到 runtime 即非本地」），
    # credential_id=0 / last_ip 前缀判定仍留在调用方（业务口径不是域数据口径）。
    "runtime_locality": (
        "SELECT ar.credential_id, rc.last_ip FROM agent_runtimes ar "
        "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id WHERE ar.id=?"),
    "runtime_engine_name": "SELECT name FROM agent_runtimes WHERE id=?",
    # ── 任务轨迹重建（insight/observability/trace）：agent_tasks / task_messages ──
    # insight 是只读分析域，轨迹取数全归本域；列序与原 insight SQL 逐字一致
    # （调用方按下标/键取值）。动态 IN 列表由下方 owner 函数分块拼占位符，
    # 调用方只传 id 列表，不接触 SQL 片段。
    "trace_tasks_unstamped": (
        "SELECT id, repo_key, issue_id FROM agent_tasks "
        "WHERE trace_id IS NULL OR trace_id='' LIMIT ?"),
    "trace_task_stamp": "UPDATE agent_tasks SET trace_id=?, pipeline_trace_id=? WHERE id=?",
    "trace_task_trace_ids": "SELECT trace_id, pipeline_trace_id FROM agent_tasks WHERE id=?",
    "trace_task_messages": (
        "SELECT seq, type, tool, content, input_json, output, created_at "
        "FROM task_messages WHERE task_id=? ORDER BY seq LIMIT ?"),
    # agent_runtimes × runtime_credentials 两表同属本域，JOIN 留在 owner 侧一条查询；
    # 行不存在 → 无行（调用方回退 `#<id>`，属展示口径不是域数据口径）。
    "trace_runtime_label": (
        "SELECT COALESCE(NULLIF(r.name,''), NULLIF(r.provider,''), ''), "
        "COALESCE(rc.device_name, rc.name, '') FROM agent_runtimes r "
        "LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id WHERE r.id=?"),
    # ── 分布式锁（distributed_locks，本域基表；原 SQL 在 src/kernel/lock.py）──
    # 列序/谓词与原语句逐字一致：抢锁是「INSERT 冲突 → 查过期 → 条件 UPDATE」三步，
    # 由下方 acquire_distributed_lock 在同一事务内完成，调用方不再持有任何 SQL。
    "lock_insert": (
        "INSERT INTO distributed_locks (lock_key, holder, locked_at, expires_at, "
        "update_at, created_at) VALUES (?, ?, ?, ?, ?, ?)"),
    "lock_row": "SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?",
    "lock_takeover": (
        "UPDATE distributed_locks SET holder=?, locked_at=?, expires_at=?, update_at=? "
        "WHERE lock_key=? AND expires_at < ?"),
    "lock_holder": "SELECT holder FROM distributed_locks WHERE lock_key=?",
    "lock_release": "DELETE FROM distributed_locks WHERE lock_key=? AND holder=?",
    "lock_renew": (
        "UPDATE distributed_locks SET expires_at=?, update_at=? "
        "WHERE lock_key=? AND holder=?"),
    "lock_cleanup": "DELETE FROM distributed_locks WHERE expires_at < ? AND holder != ?",
    # ── autopilot 执行记录 / 技能名（mgmt/routes/autopilot.py 跨域收口）──
    # 列序/谓词与原语句逐字一致；分页查询的可变部分（过滤组合、迁移兼容列）由本模块
    # 白名单挑选（见 list_autopilot_executions），调用方不再持有任何 SQL 文本。
    "autopilot_executions_delete_by_task": "DELETE FROM autopilot_executions WHERE task_id=?",
    "autopilot_findings_set": "UPDATE autopilot_executions SET findings_json=? WHERE id=?",
    "autopilot_execution_findings_result": (
        "SELECT findings_json, result FROM autopilot_executions WHERE id=?"),
    # IM 巡检执行卡（runtime/im/handler_async.py）：与 mgmt 的 autopilot_tasks 原为一条
    # 跨域 JOIN，拆成「本域取执行行 + 归属域取任务简报」后在 Python 内连接。列序照抄原
    # SELECT 中属于本域的三列（task_id 用于去归属域取 repo_key/name）。
    "autopilot_execution_detail": (
        "SELECT task_id, findings_json, result, status FROM autopilot_executions WHERE id=?"),
    "skill_display_name_official": (
        "SELECT display_name FROM skill WHERE skill_name=? AND is_active=1 AND scope='official'"),
    # ── DevStage 环境凭据清理（mgmt/routes/devstage.py 跨域收口）──
    # 三张表（runtime_credentials / agent_runtimes / devops_agent_session）全归本域。
    # 列序/谓词与原语句逐字一致：健康注记取「未吊销」全量行（不过滤 active，与原口径同）；
    # 按 env 名定位凭据（创建环境时 name = env.name）。引擎行与会话摘绑的 IN 列表
    # 在函数内分块拼装，SQL 文本只留在本域。
    "credential_health_rows": (
        "SELECT name, status, last_seen_at FROM runtime_credentials WHERE status != 'revoked'"),
    "credential_id_by_name": "SELECT id FROM runtime_credentials WHERE name=?",
    # ── 分钟/小时指标桶取数（insight/observability/metric_rollup 跨域收口）──
    # agent_tasks / events / runtime_credentials 三张表全归本域（table-owner.tsv）。
    # 列序/谓词与原 insight SQL 逐字一致（调用方按行下标取值，错位即静默串列），
    # 只把连接换成「任意拓扑」游标；`repo_key` 空值排除谓词照抄，不改口径。
    "rollup_task_counts_by_created": (
        "SELECT repo_key, status, COUNT(*) FROM agent_tasks"
        " WHERE created_at >= ? AND created_at < ?"
        " AND repo_key IS NOT NULL AND repo_key != ''"
        " GROUP BY repo_key, status"),
    "rollup_task_counts_by_completed": (
        "SELECT repo_key, status, COUNT(*) FROM agent_tasks"
        " WHERE completed_at >= ? AND completed_at < ?"
        " AND repo_key IS NOT NULL AND repo_key != ''"
        " GROUP BY repo_key, status"),
    "rollup_task_timeout_counts": (
        "SELECT repo_key, COUNT(*) FROM agent_tasks"
        " WHERE completed_at >= ? AND completed_at < ?"
        "   AND status='failed'"
        "   AND (result_text LIKE ? OR errors LIKE ?)"
        "   AND repo_key IS NOT NULL AND repo_key != ''"
        " GROUP BY repo_key"),
    "rollup_task_timing_rows": (
        "SELECT repo_key, created_at, started_at, completed_at FROM agent_tasks"
        " WHERE started_at >= ? AND started_at < ?"
        " AND repo_key IS NOT NULL AND repo_key != ''"),
    "rollup_event_cascade_counts": (
        "SELECT repo_key, COUNT(*) FROM events"
        " WHERE completed_at >= ? AND completed_at < ?"
        "   AND report_missing=1"
        "   AND repo_key IS NOT NULL AND repo_key != ''"
        " GROUP BY repo_key"),
    "rollup_online_credentials": (
        "SELECT COUNT(*) FROM runtime_credentials"
        " WHERE status != 'revoked' AND last_seen_at IS NOT NULL"
        " AND last_seen_at >= ?"),
}


@contextmanager
def _any_cursor(write: bool = False):
    """runtime 基表游标：自有 DSN 优先（autocommit），单库/测试拓扑回落进程主库。

    `write=True` 时在回落路径提交（自有 DSN 连接本身是 autocommit，无需提交）。
    """
    if db_registry.uses_own_dsn("runtime"):
        with db_registry.cursor("runtime") as c:
            yield c
        return
    from src.kernel.session_manager import db as _pdb, db_lock as _plock

    with _plock:
        c = _pdb.cursor()
        yield c
        if write:
            _pdb.commit()


def _any_sql(name: str) -> str:
    """取具名查询 SQL；未登记抛 KeyError（不静默退回任意 SQL）。"""
    sql = _ANY_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown portable query: {name}")
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
    """按具名查询执行写语句（任意拓扑），返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql(name), params)
        return int(getattr(cur, "rowcount", 0) or 0)


# ── autopilot 执行记录 / 引擎实例校验 / 技能名（mgmt/routes/autopilot.py 收口）──
#
# 这些站点原来是「lib 优先 + 本地 SQL 兜底」：兜底 SQL 留在 mgmt 文件里 → 静态面恒计
# 跨域引用；三库拓扑下 mgmt 进程库没有这些表（视图 0 → 1146，被内部 except 吞成
# 空结果/假 0），是「静默数据分裂」的典型来源。现按表上移成运行域原语。

_RUNTIME_ID_CHUNK = 400


def runtimes_credential_ids_any(ids) -> dict[int, int]:
    """按引擎实例 id 批量取 (id → credential_id)（任意拓扑）。

    原语句 `SELECT id, credential_id FROM agent_runtimes WHERE id IN (...)`；参数超过
    400 个时按 400 分块（避免撞绑定参数上限），块间无顺序依赖、结果合并即可。
    """
    vals = [int(x) for x in (ids or [])]
    out: dict[int, int] = {}
    for start in range(0, len(vals), _RUNTIME_ID_CHUNK):
        chunk = vals[start:start + _RUNTIME_ID_CHUNK]
        ph = ", ".join(["?"] * len(chunk))
        with _any_cursor() as c:
            c.execute(f"SELECT id, credential_id FROM agent_runtimes WHERE id IN ({ph})",
                      tuple(chunk))
            for r in (c.fetchall() or []):
                out[int(r[0])] = int(r[1] or 0)
    return out


def delete_autopilot_executions_by_task_any(task_id) -> int:
    """删除某巡检任务的执行记录（任意拓扑），返回删除行数。"""
    return exec_any("autopilot_executions_delete_by_task", (int(task_id),))


def set_autopilot_findings_any(exec_id, findings_json: str) -> int:
    """回写某次巡检的 findings_json（任意拓扑），返回影响行数。"""
    return exec_any("autopilot_findings_set", (findings_json, int(exec_id)))


def autopilot_execution_findings_result_any(exec_id) -> tuple | None:
    """取执行记录的 (findings_json, result)（任意拓扑）；不存在返回 None。"""
    return read_one_any("autopilot_execution_findings_result", (int(exec_id),))


def autopilot_execution_detail_any(exec_id) -> tuple | None:
    """取执行记录的 (task_id, findings_json, result, status)（任意拓扑）；无行返回 None。"""
    return read_one_any("autopilot_execution_detail", (int(exec_id),))


def skill_display_name_official_any(skill_name: str) -> str:
    """按技能名取官方启用技能的 display_name（任意拓扑）；无该技能/空名返回空串。

    原语句的两处过滤（`is_active=1` / `scope='official'`）照抄：下架技能与非官方技能
    同样取不到显示名，前端回退 Skill ID 的口径不变。
    """
    if not skill_name:
        return ""
    row = read_one_any("skill_display_name_official", (skill_name,))
    return str(row[0] or "") if row else ""


def get_meta_value_any(key: str) -> str:
    """读 meta 配置值（任意拓扑；不存在返回空串）。"""
    row = read_one_any("meta_value", (key,))
    return (row[0] or "") if row else ""


def set_meta_value_any(key: str, value: str) -> None:
    """写 meta 配置值（任意拓扑）：UPDATE→INSERT 两段式幂等 upsert。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql("meta_upsert_update"), (value, key))
        if not getattr(cur, "rowcount", 0):
            c.execute(_any_sql("meta_upsert_insert"), (key, value))


def set_meta_if_absent(key: str, value: str) -> bool:
    """meta 幂等插入（任意拓扑）：键已存在不动，返回是否真的写入。"""
    return exec_any("meta_insert_absent", (key, value)) > 0


def set_meta_values_any(items) -> int:
    """批量写 meta 配置（任意拓扑，**单事务**）：返回写入的 key 数。

    参数形如 `[("vcs_providers_config", json_str), ("global_bot_gitee_token", "x"), ...]`，
    同 key 后者覆盖前者（按传入顺序）。

    为什么是「INSERT OR IGNORE → UPDATE」而不是 REPLACE：REPLACE 走删-插语义，
    以后 meta 增列会被静默重置；而「UPDATE 命中但值未变 → rowcount=0」在 MySQL
    下会被误判成「行不存在」再 INSERT 撞主键，所以顺序固定为先补行、后改值。
    """
    pairs = [(str(k), "" if v is None else str(v)) for k, v in (items or []) if str(k or "")]
    if not pairs:
        return 0
    with _any_tx() as c:
        for key, value in pairs:
            c.execute(_any_sql("meta_insert_absent"), (key, value))
            c.execute(_any_sql("meta_upsert_update"), (value, key))
    return len(pairs)


def merge_meta_json_any(key: str, patch: dict, base: dict | None = None,
                        extra: dict | None = None) -> dict:
    """读-合并-写 meta 里的 JSON 对象（任意拓扑，**同一事务**），返回合并结果。

    合并顺序：`base`（代码内默认卡）→ meta 现有 JSON → `patch`（本次提交），
    即「默认兜底、存量为主、本次覆盖」。`extra` 里的 key/value（client_id /
    client_secret 等凭据）在同一事务内一并 upsert，**空值跳过**——编辑面「留空=不改」
    是原实现口径（`if cid: ...`），空白凭据从不清空存量。

    读改写放在一个事务里，是为了消除原实现「先读后写、中间释放锁」的丢配置窗口
    （并发两次保存时后写者会用陈旧快照覆盖前者的渠道）。存量 JSON 解析失败按
    「无存量」降级并留 debug 日志，不阻断保存。
    """
    extra_pairs = [(str(k), str(v)) for k, v in (extra or {}).items()
                   if str(k or "") and str(v or "")]
    with _any_tx() as c:
        c.execute(_any_sql("meta_value"), (key,))
        row = c.fetchone()
        merged = dict(base or {})
        raw = (row[0] or "") if row else ""
        if raw:
            try:
                loaded = json.loads(raw)
            except Exception as exc:  # noqa: BLE001 - 存量脏值降级为「无存量」
                log.debug(f"meta json parse failed: {key}: {exc}")
                loaded = None
            if isinstance(loaded, dict):
                merged.update(loaded)
        merged.update(patch or {})
        payload = json.dumps(merged, ensure_ascii=False)
        c.execute(_any_sql("meta_insert_absent"), (key, payload))
        c.execute(_any_sql("meta_upsert_update"), (payload, key))
        for extra_key, extra_value in extra_pairs:
            c.execute(_any_sql("meta_insert_absent"), (extra_key, extra_value))
            c.execute(_any_sql("meta_upsert_update"), (extra_value, extra_key))
    return merged


_FEATURED_SKILL_COLS = ("id", "skill_name", "display_name", "description", "agent_type",
                        "is_featured", "like_count", "category_id")


def _featured_skill_row(r) -> dict[str, Any]:
    return {"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
            "description": r[3] or "", "agent_type": r[4] or "",
            "is_featured": r[5], "like_count": r[6], "category_id": r[7] or 0}


def list_featured_skills_any(ordered_ids: list[int] | None = None, limit: int = 5) -> list[dict]:
    """精选技能（任意拓扑）：口径与 `list_featured_skills` 完全一致。

    ordered_ids 非空按其顺序（管理台排序），缺失的按 like_count 降序追加；为空则纯 like_count 降序。
    """
    lim = max(0, int(limit))
    if not lim:
        return []
    sel = (f"SELECT {', '.join(_FEATURED_SKILL_COLS)} FROM skill "
           "WHERE is_active=1 AND is_featured=1 AND scope='official'")
    out: list[dict] = []
    with _any_cursor() as c:
        if ordered_ids:
            ids = [int(i) for i in ordered_ids]
            ph = ",".join(["?"] * len(ids))
            c.execute(f"{sel} AND id IN ({ph})", tuple(ids))
            rows = {r[0]: r for r in c.fetchall()}
            for item_id in ids:
                r = rows.get(item_id)
                if r:
                    out.append(_featured_skill_row(r))
            if len(out) < lim:
                c.execute(f"{sel} AND id NOT IN ({ph}) ORDER BY like_count DESC", tuple(ids))
                out.extend(_featured_skill_row(r) for r in c.fetchall())
        else:
            c.execute(f"{sel} ORDER BY like_count DESC LIMIT ?", (lim,))
            out = [_featured_skill_row(r) for r in c.fetchall()]
    return out[:lim]


# ---------- P4 洞察看板聚合原语（insight 只读分析域的唯一取数口） ----------
#
# 背景：三库独立后 insight 是纯只读分析域，看板里的 agent_tasks / runtime_credentials /
# events / agent_runtimes / devops_agent_session / issue 都归 runtime 所有。直接把 SQL
# 留在 insight 里会让 insight 进程在独立拓扑下命中 1146（无跨库视图）。
#
# 约定（与 `_ANY_QUERIES` 同族）：
# - 一律走 `_any_cursor`：runtime 自有 DSN 优先，单库/测试拓扑回落进程主库——
#   因此调用方**不需要** if/else 双路径，单库与三库行为同源；
# - `?` 占位符（mysql_compat 翻译成 `%s`），不写方言私有函数（strftime 除外并带兜底）；
# - 返回 tuple/list/dict 等 JSON 友好类型，**列序 = 原 insight 端 SQL 列序**，
#   保证看板返回结构、分页、排序、字段名一个字都不变；
# - 过滤条件语义与原 SQL 一致：repo_key 候选集（跨平台前缀）+ 组织隔离 org_id。

def _rk_scope(repo_keys, org_id: int = 0, column: str = "repo_key") -> tuple[str, list[Any]]:
    """repo_key 候选集 + 组织隔离的可选过滤片段（` AND ...`）与参数。"""
    sql = ""
    args: list[Any] = []
    keys = [str(k) for k in (repo_keys or []) if k]
    if keys:
        sql += f" AND {column} IN ({','.join(['?'] * len(keys))})"
        args.extend(keys)
    if org_id:
        sql += " AND org_id=?"
        args.append(int(org_id))
    return sql, args


def query_events_any(select: str, where: str = "", params: tuple = (), order_by: str = "",
                     limit: int = 0, offset: int = 0) -> list[tuple]:
    """events 受控查询（任意拓扑）：口径与 `query_events` 完全一致，单库也可用。"""
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
    with _any_cursor() as c:
        c.execute(sql, params)
        return [tuple(r) for r in (c.fetchall() or [])]


def event_dashboard_metrics(repo_keys, week_cutoff: str = "") -> dict[str, Any]:
    """仓库看板四个事件指标一次取回（替代 insight 侧 5 条标量 SQL）。

    返回 {total_issues, completed, failed, events_week, avg_duration}：
    - total_issues：DISTINCT issue_id（非空）计数；
    - completed / failed：status 命中计数（auto_resolution 分母的分子分母）；
    - events_week：created_at >= 应用层算好的 cutoff；
    - avg_duration：completed 且 duration>0 的 AVG(duration)，无样本返回 None。
    """
    where = "repo_key IN (" + ",".join(["?"] * max(1, len(repo_keys))) + ")"
    args = tuple(str(k) for k in repo_keys)
    out: dict[str, Any] = {"total_issues": 0, "completed": 0, "failed": 0,
                           "events_week": 0, "avg_duration": None}
    with _any_cursor() as c:
        c.execute(f"SELECT COUNT(DISTINCT issue_id) FROM events WHERE {where} "
                  "AND issue_id!='' AND issue_id IS NOT NULL", args)
        row = c.fetchone()
        out["total_issues"] = int((row or [0])[0] or 0)
        c.execute(f"SELECT COUNT(*) FROM events WHERE {where} AND status='completed'", args)
        row = c.fetchone()
        out["completed"] = int((row or [0])[0] or 0)
        c.execute(f"SELECT COUNT(*) FROM events WHERE {where} AND status='failed'", args)
        row = c.fetchone()
        out["failed"] = int((row or [0])[0] or 0)
        c.execute(f"SELECT COUNT(*) FROM events WHERE {where} AND created_at >= ?",
                  args + (week_cutoff,))
        row = c.fetchone()
        out["events_week"] = int((row or [0])[0] or 0)
        c.execute(f"SELECT AVG(duration) FROM events WHERE {where} AND status='completed' "
                  "AND duration IS NOT NULL AND duration > 0", args)
        row = c.fetchone()
        out["avg_duration"] = (row or [None])[0]
    return out


def dashboard_task_status_counts(repo_keys=(), org_id: int = 0) -> list[tuple]:
    """任务队列状态分布：[(status, count)]（GROUP BY status，口径同 ops overview）。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute("SELECT status, COUNT(*) FROM agent_tasks WHERE 1=1" + where +
                  " GROUP BY status", tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_issue_status_counts(repo_keys=(), org_id: int = 0) -> list[tuple]:
    """issue 级状态分布：每个 issue 取 MAX(id) 那条 task 的状态再计数。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute(
            "SELECT t.status, COUNT(*) FROM ("
            " SELECT issue_id, MAX(id) as max_id FROM agent_tasks"
            " WHERE issue_id IS NOT NULL AND issue_id <> ''" + where +
            " GROUP BY issue_id) latest JOIN agent_tasks t ON t.id = latest.max_id"
            " GROUP BY t.status", tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_stuck_tasks(cutoff: str, repo_keys=(), org_id: int = 0,
                          limit: int = 100) -> list[tuple]:
    """stuck 任务（running 且 started_at < cutoff），按 started_at 升序。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute(
            "SELECT id, agent_type, runtime_id, started_at, repo_key, issue_id, event_seq "
            "FROM agent_tasks WHERE status='running' AND started_at IS NOT NULL "
            "AND started_at < ?" + where + " ORDER BY started_at ASC LIMIT " + str(int(limit)),
            (cutoff,) + tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_failed_tasks(repo_keys=(), org_id: int = 0) -> list[tuple]:
    """最近 failed 任务（按 completed_at 降序；调用方自行截断，保持原无 LIMIT 口径）。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute(
            "SELECT id, agent_type, runtime_id, started_at, completed_at, repo_key, issue_id, "
            "event_seq, result_text, exit_code, errors FROM agent_tasks WHERE status='failed'"
            + where + " ORDER BY completed_at DESC", tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_task_by_source(repo_keys=(), org_id: int = 0) -> list[tuple]:
    """按 agent_type × status 的排队/运行数（只含 queued/dispatched/running）。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute(
            "SELECT agent_type, status, COUNT(*) FROM agent_tasks "
            "WHERE status IN ('queued','dispatched','running')" + where +
            " GROUP BY agent_type, status", tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_today_status_counts(day_prefix: str, repo_keys=(), org_id: int = 0) -> list[tuple]:
    """今日任务状态分布（UTC 日期前缀匹配 created_at）。"""
    where, args = _rk_scope(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute("SELECT status, COUNT(*) FROM agent_tasks WHERE created_at LIKE ?" + where +
                  " GROUP BY status", (f"{day_prefix}%",) + tuple(args))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_running_counts(org_id: int = 0) -> dict[int, int]:
    """每个引擎当前 running 任务数（实时负载）：{runtime_id: n}。"""
    where, args = _rk_scope((), org_id)
    with _any_cursor() as c:
        c.execute("SELECT runtime_id, COUNT(*) FROM agent_tasks WHERE status='running'" + where +
                  " GROUP BY runtime_id", tuple(args))
        return {r[0]: int(r[1] or 0) for r in (c.fetchall() or [])}


def dashboard_avg_durations(org_id: int = 0) -> dict[int, float]:
    """每个引擎已完成任务的平均耗时（秒）：{runtime_id: seconds}，双方言兜底。"""
    where, args = _rk_scope((), org_id)
    try:
        with _any_cursor() as c:
            c.execute(
                "SELECT runtime_id, AVG(CASE WHEN completed_at IS NOT NULL "
                "AND started_at IS NOT NULL THEN (strftime('%s', completed_at) - "
                "strftime('%s', started_at)) ELSE NULL END) as avg_sec "
                "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0"
                + where + " GROUP BY runtime_id", tuple(args))
            return {r[0]: round(r[1] or 0, 1) if r[1] else 0 for r in (c.fetchall() or [])}
    except Exception:  # noqa: BLE001 - sqlite strftime 在 MySQL 上是 1064，走下面的口径
        with _any_cursor() as c:
            c.execute(
                "SELECT runtime_id, AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) as avg_sec "
                "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0 "
                "AND started_at IS NOT NULL AND completed_at IS NOT NULL" + where +
                " GROUP BY runtime_id", tuple(args))
            return {r[0]: round(r[1] or 0, 1) if r[1] else 0 for r in (c.fetchall() or [])}


_WORKER_COLS = ("id, name, device_name, status, last_seen_at, last_ip, "
                "cpu_percent, memory_percent, disk_percent, tmp_percent, "
                "bandwidth_percent, bandwidth_mbps, load_avg, owner_name")


def dashboard_workers(stale_cutoff: str, org_id: int = 0, uname: str = "",
                      repo_key: str = "", repo: str = "") -> list[tuple]:
    """看板 worker（凭据）列表：14 列，列序与 insight 原 SQL 一致。

    可见性口径（与原三份 SQL 逐字对齐）：
    - 非个人组织（org_id>0）：只看 org_id 匹配的凭据；
    - 仓库级看板（repo 非空）：global + 自己的 user + 该 repo 的凭据；
    - 个人组织全局看板：global + 自己的 user。
    三档共同前置：未吊销（status!='revoked'）且 7 天内有心跳。
    """
    base = (f"SELECT {_WORKER_COLS} FROM runtime_credentials WHERE status != 'revoked' "
            "AND last_seen_at IS NOT NULL AND last_seen_at >= ?")
    if org_id:
        sql = base + " AND org_id=? ORDER BY id"
        args: tuple = (stale_cutoff, int(org_id))
    elif repo:
        sql = base + (" AND (scope='global' OR (scope='user' AND owner_name=?) "
                      "OR repo_key=? OR repo_key=?) ORDER BY id")
        args = (stale_cutoff, uname, repo_key, repo)
    else:
        sql = base + " AND (scope='global' OR (scope='user' AND owner_name=?)) ORDER BY id"
        args = (stale_cutoff, uname)
    with _any_cursor() as c:
        c.execute(sql, args)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_local_runtimes(org_id: int = 0) -> list[tuple]:
    """本机直连引擎（credential_id=0）：7 列，供看板补一条本地 worker。"""
    sql = ("SELECT ar.id, ar.name, ar.status, ar.last_seen_at, ar.host_name, "
           "ar.healthy, ar.busy_slots, ar.max_concurrent FROM agent_runtimes ar "
           "WHERE ar.credential_id = 0")
    args: tuple = ()
    if org_id:
        sql += " AND ar.org_id=?"
        args = (int(org_id),)
    with _any_cursor() as c:
        c.execute(sql, args)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_runtimes_with_credential(org_id: int = 0) -> list[tuple]:
    """引擎列表 + 绑定凭据（20 列）：列序与 insight 原 LEFT JOIN 完全一致。

    含 revoked/孤儿判定所需的 `rc.status AS cred_status` 与 `rc.id AS cred_pk`：
    调用方据此跳过已吊销凭据的引擎（Issue #417），过滤语义留在调用方。
    """
    sql = ("SELECT ar.id, ar.name, ar.credential_id, ar.status, ar.last_seen_at, ar.provider, "
           "ar.healthy, ar.busy_slots, ar.max_concurrent, ar.version, "
           "rc.name AS rc_name, rc.device_name, rc.status AS cred_status, rc.id AS cred_pk, "
           "ar.host_name, ar.probe_status, ar.probe_latency, ar.probe_last, ar.probe_error, "
           "ar.org_id FROM agent_runtimes ar "
           "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id")
    args: tuple = ()
    if org_id:
        sql += " WHERE ar.org_id=?"
        args = (int(org_id),)
    sql += " ORDER BY ar.id"
    with _any_cursor() as c:
        c.execute(sql, args)
        return [tuple(r) for r in (c.fetchall() or [])]


def credential_brief(cred_id) -> tuple | None:
    """凭据摘要 (name, device_name)：仅两列，不含 token_hash；不存在返回 None。"""
    with _any_cursor() as c:
        c.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (cred_id,))
        row = c.fetchone()
        return tuple(row) if row else None


def credential_scope_owner(cred_id) -> tuple | None:
    """凭据可见性 (scope, owner_name)：供看板做 user scope 归属过滤。"""
    with _any_cursor() as c:
        c.execute("SELECT scope, owner_name FROM runtime_credentials WHERE id=?", (cred_id,))
        row = c.fetchone()
        return tuple(row) if row else None


def task_event_map() -> list[tuple]:
    """(issue_id, event_seq) → (task_id, runtime_id) 映射源：timeline 一次取全。"""
    with _any_cursor() as c:
        c.execute("SELECT issue_id, event_seq, id, runtime_id FROM agent_tasks "
                  "WHERE issue_id IS NOT NULL AND issue_id <> ''")
        return [tuple(r) for r in (c.fetchall() or [])]


def runtime_device_map() -> list[tuple]:
    """引擎 → 设备名映射源 (ar.id, ar.name, ar.credential_id, rc.device_name, ar.host_name)。"""
    with _any_cursor() as c:
        c.execute("SELECT ar.id, ar.name, ar.credential_id, rc.device_name, ar.host_name "
                  "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc "
                  "ON ar.credential_id = rc.id")
        return [tuple(r) for r in (c.fetchall() or [])]


def runtime_locality(runtime_id) -> tuple | None:
    """引擎本地性判定源 (credential_id, last_ip)；runtime 行不存在返回 None。

    改造前调用方连查两次（agent_runtimes → runtime_credentials），第二句在无凭据行时
    返回 None 行 → 判定为「非本地」；LEFT JOIN 的行不存在语义与之等价。
    「credential_id=0 → 本机直连」「last_ip 前缀 127. → 本机 worker」属业务口径，留在调用方。
    """
    row = read_one_any("runtime_locality", (runtime_id,))
    return tuple(row) if row else None


def runtime_engine_name(runtime_id) -> str:
    """引擎名（agent_runtimes.name）；行不存在返回空串（兜底文案由调用方决定）。"""
    row = read_one_any("runtime_engine_name", (runtime_id,))
    return str(row[0] or "") if row else ""


def agent_sessions_with_runtime(repo_keys) -> list[tuple]:
    """看板 Agent 卡片会话行 (agent_type, runtime.name, status, last_active_at, enable,
    credential.status)：6 列，列序与 insight 原三表 JOIN 一致。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(
            "SELECT s.agent_type, r.name, s.status, s.last_active_at, s.enable, "
            "COALESCE(rc.status,'') FROM devops_agent_session s "
            "LEFT JOIN agent_runtimes r ON s.runtime_id=r.id "
            "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
            f"WHERE s.repo_key IN ({ph}) ORDER BY s.agent_type", tuple(keys))
        return [tuple(r) for r in (c.fetchall() or [])]


def efficiency_issue_contrib(repo_keys) -> list[tuple]:
    """能效看板 issue 侧聚合 (creator, assignee, user_id, count, completed)。

    分组口径：COALESCE(NULLIF(created_by,''),'') / COALESCE(assignee,'') /
    COALESCE(user_id,0)，排除子任务行（is_subtask_mode=1）。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(
            "SELECT COALESCE(NULLIF(created_by,''), '') AS creator,"
            " COALESCE(assignee,'') AS assignee, COALESCE(user_id,0),"
            " COUNT(*), SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)"
            f" FROM issue WHERE repo_key IN ({ph}) AND is_subtask_mode=0"
            " GROUP BY creator, assignee, COALESCE(user_id,0)", tuple(keys))
        return [tuple(r) for r in (c.fetchall() or [])]


def task_issue_attribution(task_ids) -> list[tuple]:
    """任务 → 需求归属链 (task_id, created_by, assignee, user_id, is_subtask_mode, issue_id)。

    单条 JOIN 承载不了分块（SQLite 变量上限），故分块落在 owner 域内部：
    调用方只给 task_id 列表，500 一段，返回顺序与入参一致。
    """
    ids = [int(t) for t in (task_ids or [])]
    if not ids:
        return []
    out: list[tuple] = []
    with _any_cursor() as c:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            ph = ",".join(["?"] * len(part))
            c.execute(
                "SELECT t.id, i.created_by, i.assignee, COALESCE(i.user_id, 0), "
                "i.is_subtask_mode, i.id FROM agent_tasks t LEFT JOIN issue i "
                "ON i.repo_key = t.repo_key AND i.issue_number = "
                f"CAST(SUBSTR(t.issue_id, 2) AS INTEGER) WHERE t.id IN ({ph})",
                tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


# ── 任务轨迹重建原语（insight/observability/trace 的唯一取数口）──────────────
#
# 轨迹把「任务 → 消息步骤 → 引擎标注 → 流水线事件」拼成一条时间线，四张表
# （agent_tasks / task_messages / events / agent_runtimes×runtime_credentials）
# 全归本域。insight 侧只保留确定性 trace id 生成、时间换算与可见性过滤（业务语义），
# SQL 一律在此：三库拓扑下 insight 进程没有这些表（无视图 → 1146），单库测试拓扑
# 也必须跑同一份语句，因此全部走 `_any_cursor`（自有 DSN 优先，未配回落进程主库）。

_TRACE_TASK_COLS = ("id", "agent_type", "status", "issue_id", "repo_key", "runtime_id",
                    "event_seq", "created_at", "started_at", "completed_at", "errors",
                    "stderr", "result_text", "trace_id", "pipeline_trace_id")
_TRACE_TASK_SELECT = ", ".join(_TRACE_TASK_COLS)
_TRACE_STEP_COLS = ("seq", "type", "tool", "content", "input_json", "output", "created_at")
_TRACE_EVENT_COLS = ("seq", "event_type", "actor", "issue_id", "title", "status", "created_at")
_TRACE_IN_CHUNK = 500


def _trace_dicts(cols: tuple, rows) -> list[dict[str, Any]]:
    """行 → dict（列名由本文件定义，列序即契约）。"""
    return [dict(zip(cols, r)) for r in (rows or [])]


def trace_unstamped_tasks(limit: int = 5000) -> list[tuple]:
    """待回填 trace id 的任务 [(id, repo_key, issue_id)]（原 `trace_id IS NULL OR ''` 口径）。"""
    return [tuple(r) for r in read_all_any("trace_tasks_unstamped", (int(limit),))]


def stamp_task_traces_any(pairs: list[tuple]) -> int:
    """批量补写 agent_tasks 的 trace_id / pipeline_trace_id（任意拓扑，单事务，幂等）。

    与 `stamp_task_traces`（自有 DSN 版、`%s` 方言）差别只有连接与方言：同一批在一个
    事务里提交（原来靠调用方 db_lock + 末尾 commit），单条脏 id 只跳过该条不阻断整批。
    返回真正改到的行数（rowcount；写不存在的任务不计，比旧口径「每执行一行 +1」更准）。
    """
    if not pairs:
        return 0
    done = 0
    with _any_tx() as c:
        for tid, tid_trace, ptrace in pairs:
            try:
                cur = c.execute(_any_sql("trace_task_stamp"), (tid_trace, ptrace, str(tid)))
                done += int(getattr(cur, "rowcount", 0) or 0)
            except Exception as exc:  # noqa: BLE001 - 单条脏数据不阻断整批回填
                log.debug(f"trace stamp skipped task={tid}: {exc}")
    return done


def trace_tasks_for_trace(trace: str) -> list[dict[str, Any]]:
    """按 trace 值取任务行（trace_id 或 pipeline_trace_id 命中），列序 = 原 insight SQL。"""
    t = str(trace or "")
    with _any_cursor() as c:
        c.execute(f"SELECT {_TRACE_TASK_SELECT} FROM agent_tasks "
                  "WHERE trace_id=? OR pipeline_trace_id=? ORDER BY created_at", (t, t))
        return _trace_dicts(_TRACE_TASK_COLS, c.fetchall())


def trace_tasks_for_task(task_id: str) -> list[dict[str, Any]]:
    """任务号 → 该任务整条流水线（同 trace / pipeline 的其他任务一并返回）。

    先按 id 定位它的 trace_id / pipeline_trace_id，再按候选集取行；定位不到 → 空列表
    （原口径：不回落全表扫描，未知任务号与「不存在」等价）。
    """
    tid = str(task_id or "")
    if not tid:
        return []
    got = read_one_any("trace_task_trace_ids", (tid,))
    if not got:
        return []
    keys = [k for k in (tid, got[0] or "", got[1] or "") if k]
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(f"SELECT {_TRACE_TASK_SELECT} FROM agent_tasks "
                  f"WHERE trace_id IN ({ph}) OR pipeline_trace_id IN ({ph}) OR id=? "
                  "ORDER BY created_at", tuple(keys + keys + [tid]))
        return _trace_dicts(_TRACE_TASK_COLS, c.fetchall())


def trace_task_steps(task_id: str, limit: int) -> list[dict[str, Any]]:
    """任务消息步骤（按 seq 升序；LIMIT 由调用方给，原 STEP_LIMIT 口径）。"""
    return _trace_dicts(_TRACE_STEP_COLS,
                        read_all_any("trace_task_messages", (str(task_id), int(limit))))


def trace_runtime_label(runtime_id: int) -> dict[str, str]:
    """引擎标注 {engine, device}：name 优先退 provider，设备名退凭据 name；无行 → 空 dict。"""
    row = read_one_any("trace_runtime_label", (int(runtime_id),))
    if not row:
        return {}
    return {"engine": str(row[0] or ""), "device": str(row[1] or "")}


def trace_events_by_seq(seqs) -> list[dict[str, Any]]:
    """同一流水线的事件上下文（seq 候选集，按 seq 升序）。

    seq 列表可能上千（单任务步骤上限 200，流水线再乘任务数），按 500 分块；
    跨块结果最后再整体按 seq 排序，保证与单条 `ORDER BY seq` 的输出顺序一致。
    """
    ids = [int(s) for s in (seqs or []) if s]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    with _any_cursor() as c:
        for i in range(0, len(ids), _TRACE_IN_CHUNK):
            part = ids[i:i + _TRACE_IN_CHUNK]
            ph = ",".join(["?"] * len(part))
            c.execute(f"SELECT {', '.join(_TRACE_EVENT_COLS)} FROM events "
                      f"WHERE seq IN ({ph}) ORDER BY seq", tuple(part))
            out.extend(_trace_dicts(_TRACE_EVENT_COLS, c.fetchall()))
    if len(ids) > _TRACE_IN_CHUNK:
        out.sort(key=lambda r: r["seq"])
    return out


# ── session_store 去跨域原语 ─────────────────────────────────────────────

def ensure_issue_card(repo_key: str, issue_number, assignee: str = "",
                      um_column: str = "gitcode_user") -> bool:
    """issue 卡片行建档（幂等）：无行则建最小行（status='active'）。返回是否新建。

    user_id 的解析归 **mgmt**（unified_user_mapping + grape_accounts 校验）：原来的
    `INSERT ... SELECT COALESCE((SELECT m.user_id ... EXISTS(账号表校验)))`
    是跨域子查询，三库独立后无法执行；改为先取 user_id（mgmt 原语）再插入（本域）。
    """
    if read_one_any("issue_card_exists", (repo_key, issue_number)):
        return False
    from src.domains.mgmt import public as _mgmt_uid

    try:
        _uid = _mgmt_uid.mapped_user_id(assignee, um_column=um_column)
    except Exception as exc:  # noqa: BLE001 - 身份解析失败按 "无绑定用户" 处理（原 COALESCE 语义）
        log.warning(f"ensure_issue_card: user_id lookup failed ({assignee}): {exc}")
        _uid = 0
    try:
        exec_any("issue_card_ensure_insert_plain", (_uid, assignee, repo_key, issue_number, assignee))
        return True
    except Exception:
        # 并发下唯一键冲突 → 行已由对端建好，等同"无行才建"的幂等语义
        return False

def update_issue_card_fields(repo_key: str, issue_number, fields: dict,
                             touch_updated_at: bool = True) -> int:
    """按 repo_key+issue_number 更新 issue 卡片行（列名白名单由调用方传固定字段名）。"""
    if not fields:
        return 0
    sets = ", ".join(f"{k}=?" for k in fields)
    if touch_updated_at:
        sets += ", updated_at=datetime('now')"
    params = list(fields.values()) + [repo_key, issue_number]
    with _any_cursor(write=True) as c:
        cur = c.execute(f"UPDATE issue SET {sets} WHERE repo_key=? AND issue_number=?", params)
        return int(getattr(cur, "rowcount", 0) or 0)

def ensure_agent_session(session_id: str, repo_key: str, agent_type: str, runtime_id: int = 0,
                         skill: str = "", skill_id: int = 0, repo_gitcode_token: str = "",
                         status: str = "idle", enable: int = 1,
                         subscribe_events: str = "[]") -> None:
    """建 repo 级 agent 会话行（id 由调用方生成，保证主键稳定）。"""
    exec_any("devops_agent_session_insert",
             (session_id, repo_key, agent_type, runtime_id, skill, skill_id,
              repo_gitcode_token or "", status, enable, subscribe_events))

def update_agent_session_fields(session_id: str, fields: dict,
                                touch_last_active: bool = True) -> int:
    """按 id 更新 devops_agent_session（动态字段集；last_active_at 默认同步刷新）。"""
    if not fields:
        return 0
    sets = ", ".join(f"{k}=?" for k in fields)
    if touch_last_active:
        sets += ", last_active_at=datetime('now')"
    params = list(fields.values()) + [session_id]
    with _any_cursor(write=True) as c:
        cur = c.execute(f"UPDATE devops_agent_session SET {sets} WHERE id=?", params)
        return int(getattr(cur, "rowcount", 0) or 0)

def delete_agent_sessions(repo_key: str, agent_type: str = "") -> int:
    """删 repo 级 agent 会话；agent_type 为空 → 删该仓库全部（返回删除行数）。"""
    if agent_type:
        return exec_any("devops_agent_session_delete_by_repo_agent", (repo_key, agent_type))
    return exec_any("devops_agent_session_delete_by_repo", (repo_key,))

def update_issue_agent_session_fields(session_id: str, fields: dict,
                                      touch_last_active: bool = True) -> int:
    """按 id 更新 issue_agent_session（动态字段集；last_active_at 默认同步刷新）。"""
    if not fields:
        return 0
    sets = ", ".join(f"{k}=?" for k in fields)
    if touch_last_active:
        sets += ", last_active_at=datetime('now')"
    params = list(fields.values()) + [session_id]
    with _any_cursor(write=True) as c:
        cur = c.execute(f"UPDATE issue_agent_session SET {sets} WHERE id=?", params)
        return int(getattr(cur, "rowcount", 0) or 0)


# ── session_manager 去跨域原语（kernel 侧会话/消息/群绑定/配置行） ─────────────
#
# kernel/session_manager 是「进程主库」入口，会话、消息、群绑定、Chat 配置行都归运行域。
# 三库独立后（视图 0）它必须按表走 owner 域：SQL 只留本文件一份，连接按拓扑自适应。
# 多语句组合写用 `_any_tx()`（自有 DSN → 独立非 autocommit 连接，真事务；
# 单库/测试拓扑 → 进程库 + db_lock，与原 kernel 语义一致）。


@contextmanager
def _any_tx():
    """运行域组合写的任意拓扑事务游标（多语句原子）。"""
    if db_registry.uses_own_dsn("runtime"):
        with db_registry.transaction("runtime") as c:
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
                log.debug(f"runtime public rollback skipped: {exc}")
            raise


def upsert_chat_config_row(user_id: int, runtime_id: int = 0, skill: str = "",
                           skill_id: int = 0) -> None:
    """写用户全局 Chat 配置行（`sess-cfg-<uid>`）：先收敛同用户其它配置行，再 upsert。"""
    sid = f"sess-cfg-{user_id}"
    with _any_tx() as c:
        c.execute(_any_sql("chat_config_delete_others"), (user_id, sid))
        c.execute(_any_sql("chat_config_upsert"), (sid, user_id, "", runtime_id, skill, skill_id))


def join_feishu_chat(chat_id: str, conv_id: str, user_id) -> bool:
    """飞书接入指针原子切换：校验属主 → 清旧持有者 → 设新（同一事务）。

    与原 kernel 实现等价：目标会话不存在或不属于该用户时返回 False，且不做任何写。
    """
    with _any_tx() as c:
        c.execute(_any_sql("conversation_owner"), (conv_id,))
        row = c.fetchone()
        if not row or row[0] != user_id:
            return False
        c.execute(_any_sql("conversation_clear_im_channel"), (chat_id, conv_id))
        c.execute(_any_sql("conversation_set_im_channel"), (chat_id, conv_id))
    return True


_CONV_WRITABLE = {"title", "status", "runtime_session_id", "agent_session_id",
                  "user_gitcode_token", "conv_type", "issue_number", "im_channel_id"}


def update_conversation_fields(conv_id: str, fields: dict) -> int:
    """按 id 更新会话可写列（动态字段集；列名经白名单校验，`updated_at` 同步刷新）。"""
    data = {k: v for k, v in (fields or {}).items() if k in _CONV_WRITABLE}
    if not data:
        return 0
    sets = ", ".join(f"{k}=?" for k in data)
    params = list(data.values()) + [conv_id]
    with _any_cursor(write=True) as c:
        cur = c.execute(
            f"UPDATE chat_conversations SET {sets}, updated_at=datetime('now') WHERE id=?", params)
        return int(getattr(cur, "rowcount", 0) or 0)


def append_chat_message(conversation_id: str, role: str, content: str, task_id: str = "",
                        generate_status=None) -> int:
    """追加一条会话消息，返回自增主键（task_id 为空走三列插入，与原实现分支一致）。"""
    with _any_cursor(write=True) as c:
        if task_id:
            cur = c.execute(_any_sql("message_insert_with_task"),
                            (conversation_id, role, content, task_id, generate_status))
        else:
            cur = c.execute(_any_sql("message_insert"), (conversation_id, role, content))
        return int(getattr(cur, "lastrowid", 0) or 0)


def mark_issue_processing(conv_id: str, user_id, repo_key: str, issue_number) -> None:
    """会话收到真实用户消息 → 该用户任务 active→processing（按会话优先，退化按仓库+序号）。"""
    with _any_tx() as c:
        cur = c.execute(_any_sql("issue_processing_by_conversation"), (conv_id, user_id))
        if not getattr(cur, "rowcount", 0):
            c.execute(_any_sql("issue_processing_by_number"), (repo_key, issue_number, user_id))


def upsert_repo_im_binding(chat_id: str, repo_key: str, source: str, bind_by: str,
                           app_id: str) -> int:
    """群 ↔ 仓库绑定 UPSERT：只更新绑定列，保留用户的通知/自动实现开关。"""
    return exec_any("repo_im_binding_upsert",
                    (chat_id, repo_key, source, str(bind_by), app_id))


# ── 运行域表结构引导（表归本域；DDL 落调用方连接，与 mgmt.ensure_user_mapping_schema 同口径）──

CHAT_AGENT_SESSION_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS chat_agent_session (
        id TEXT PRIMARY KEY,
        user_id TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        source TEXT, repository TEXT, status TEXT,
        runtime TEXT DEFAULT 'opencode',
        skill TEXT DEFAULT '',
        event_history MEDIUMTEXT DEFAULT '[]',
        created_at TEXT, last_active_at TEXT
    )
"""

REPO_IM_GROUP_BINDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS repo_im_group_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    im_provider TEXT DEFAULT 'feishu',
    im_chat_id TEXT NOT NULL,
    im_channel_name TEXT DEFAULT '',
    repo_key TEXT NOT NULL,
    bind_by TEXT DEFAULT '',
    im_app_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    auto_impl_enabled INTEGER DEFAULT 0,
    notify_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (im_provider, im_chat_id, im_app_id)
)
"""

#: 兼容列（老库升级补齐；幂等 ADD，列已存在时由方言层报错并吞掉）
_CHAT_SESSION_LEGACY_COLUMNS = (
    ("skill_id", "INTEGER DEFAULT 0"),
    ("skill", "TEXT DEFAULT ''"),
    ("user_id", "INTEGER DEFAULT 0"),
    ("runtime_session_id", "TEXT DEFAULT ''"),
)
_REPO_IM_LEGACY_COLUMNS = (
    ("auto_impl_enabled", "INTEGER DEFAULT 0"),
    ("notify_enabled", "INTEGER DEFAULT 0"),
    ("im_app_id", "TEXT DEFAULT ''"),
    ("im_provider", "TEXT DEFAULT 'feishu'"),
    ("im_channel_name", "TEXT DEFAULT ''"),
    ("source", "TEXT DEFAULT ''"),
)


def chat_session_ddl() -> str:
    """chat_agent_session 建表 DDL（只读快照，供文档/测试比对）。"""
    return CHAT_AGENT_SESSION_TABLE_SQL


def repo_im_binding_ddl() -> str:
    """repo_im_group_binding 建表 DDL（只读快照，供文档/测试比对）。"""
    return REPO_IM_GROUP_BINDING_TABLE_SQL


def _add_legacy_columns(conn: Any, table: str, columns: tuple) -> None:
    """幂等补列：逐列 ADD，失败只告警（列已存在 / 方言不支持生成列）。"""
    for name, decl in columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        except Exception as exc:  # noqa: BLE001 - 列已存在是正常路径
            log.debug(f"legacy column {table}.{name} skipped: {exc}")


def ensure_chat_session_schema(conn: Any) -> None:
    """在调用方连接上引导 chat_agent_session（会话主表）。

    步骤与容错口径与 kernel 原启动块逐条等价：
      1) 旧表 sessions → chat_agent_session（改名；两表并存时搬数据后 DROP 旧表），失败仅告警；
      2) CREATE TABLE IF NOT EXISTS（失败必须抛出，与原启动块一致）；
      3) 条件式与无条件式兼容列补齐（runtime_session_id / runtime_id / skill_id / runtime / skill / user_id）。
    conn 必填（DDL 必须落在调用方所在库，不能走跨域连接）。
    """
    if conn is None:
        raise ValueError("ensure_chat_session_schema 需要调用方连接（本地库引导）")
    try:
        old = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "sessions" in old and "chat_agent_session" not in old:
            conn.execute("ALTER TABLE sessions RENAME TO chat_agent_session")
            conn.commit()
            log.info("Migrated table sessions -> chat_agent_session")
        elif "sessions" in old and "chat_agent_session" in old:
            conn.execute(
                "INSERT OR IGNORE INTO chat_agent_session (id, repo_key, source, repository, "
                "status, event_history, oc_session_id, created_at, last_active_at) "
                "SELECT id, repo_key, source, repository, status, event_history, oc_session_id, "
                "created_at, last_active_at FROM sessions")
            conn.execute("DROP TABLE sessions")
            conn.commit()
            log.info("Migrated sessions rows into chat_agent_session and dropped legacy table")
    except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断启动（原实现同样只告警）
        log.warning(f"Migrate sessions table failed: {exc}")
    conn.execute(CHAT_AGENT_SESSION_TABLE_SQL)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
        if "runtime_session_id" not in cols:
            conn.execute(
                "ALTER TABLE chat_agent_session ADD COLUMN runtime_session_id TEXT DEFAULT ''")
        if "runtime_id" not in cols:
            conn.execute("ALTER TABLE chat_agent_session ADD COLUMN runtime_id INTEGER DEFAULT 0")
        if "backend" not in cols and "runtime" not in cols:
            conn.execute(
                "ALTER TABLE chat_agent_session ADD COLUMN runtime TEXT DEFAULT 'opencode'")
    except Exception as exc:  # noqa: BLE001 - 方言不支持 PRAGMA 时由下方幂等 ADD 兜底
        log.warning(f"condition-check chat_agent_session columns failed: {exc}")
    _add_legacy_columns(conn, "chat_agent_session", _CHAT_SESSION_LEGACY_COLUMNS)
    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"commit chat_agent_session schema failed: {exc}")


def ensure_repo_im_binding_schema(conn: Any) -> None:
    """在调用方连接上引导 repo_im_group_binding（群↔仓库绑定）。

    步骤：旧表 feishu_bindings 改名（幂等）→ CREATE → 兼容列补齐；废弃表 feishu_users 直接 DROP。
    conn 必填（DDL 必须落在调用方所在库）。
    """
    if conn is None:
        raise ValueError("ensure_repo_im_binding_schema 需要调用方连接（本地库引导）")
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "feishu_bindings" in tables and "repo_im_group_binding" not in tables:
            conn.execute("ALTER TABLE feishu_bindings RENAME TO repo_im_group_binding")
            conn.commit()
            log.info("Migrated table feishu_bindings -> repo_im_group_binding")
    except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断启动
        log.warning(f"Migrate feishu_bindings table failed: {exc}")
    conn.execute(REPO_IM_GROUP_BINDING_TABLE_SQL)
    _add_legacy_columns(conn, "repo_im_group_binding", _REPO_IM_LEGACY_COLUMNS)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "feishu_users" in tables:
            conn.execute("DROP TABLE feishu_users")
            conn.commit()
            log.info("Dropped legacy table feishu_users")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Drop feishu_users table failed: {exc}")
    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"commit repo_im_group_binding schema failed: {exc}")


# ---------- 迁移期批量回填（跨域 JOIN 的替代：映射由 mgmt 取，回填落本域） ----------

#: 跟随 repo_key 的 runtime 表（固定白名单：不接受调用方传表名）
_ORG_BACKFILL_TABLES = ("events", "agent_tasks", "devops_agent_session", "issue")


def backfill_org_id_by_repo(mapping: dict) -> dict[str, int]:
    """按 repo_key→org_id 映射回填本域表的空 org_id（逐仓库 UPDATE，幂等）。"""
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


def backfill_skill_category_id(category_map: dict) -> int:
    """技能分类回填（Issue #179）：sub_scene → category_id（映射由 mgmt 取，本域更新）。

    与原实现（本域 UPDATE + 分类表按 slug 关联子查询）等价：无匹配 slug 的技能保持 0。
    """
    total = 0
    if not category_map:
        return total
    with _any_tx() as c:
        for slug, cid in category_map.items():
            cur = c.execute(
                "UPDATE skill SET category_id=? WHERE sub_scene=? AND category_id=0",
                (int(cid), slug))
            total += int(getattr(cur, "rowcount", 0) or 0)
    return total


# ---------- 巡检读/写原语（meta 租约、批量回捞；表归 runtime） ----------

def _meta_lease_expired(value: str, cutoff_iso: str) -> bool:
    """meta 租约值过期判定：`holder|iso` 取时间戳段；兼容历史纯 ISO 值。"""
    if not value:
        return True
    ts = value.split("|", 1)[1] if "|" in value else value
    return ts < cutoff_iso


def claim_meta_lease(key: str, holder: str, new_val: str, cutoff_iso: str) -> bool:
    """meta 键的租约抢占（巡检主选举）：自续约 → 读现值 → CAS → 引导期 INSERT。

    四步必须在**同一事务**内完成（拆分会让两次 UPDATE 之间被他人插入/接管）：
      1) 前缀精确等于 `holder|` 即自续约（substr 而非 LIKE —— holder 含 `_`/`%` 时不误匹配）；
      2) 现值新鲜且非本主 → 让位；
      3) CAS：仅当值仍是刚读到的那份才写；
      4) 行不存在时 INSERT OR IGNORE（并发由主键裁决）。
    """
    with _any_tx() as c:
        cur = c.execute(
            "UPDATE meta SET value=? WHERE key=? AND substr(value,1,?)=?",
            (new_val, key, len(holder) + 1, holder + "|"))
        if int(getattr(cur, "rowcount", 0) or 0) == 1:
            return True
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        cur_val = (row[0] or "") if row else ""
        if cur_val and not _meta_lease_expired(cur_val, cutoff_iso):
            return False
        cur2 = c.execute("UPDATE meta SET value=? WHERE key=? AND value=?",
                         (new_val, key, cur_val))
        if int(getattr(cur2, "rowcount", 0) or 0) == 1:
            return True
        if not cur_val:
            cur3 = c.execute("INSERT INTO meta (key, value) VALUES (?,?)", (key, new_val))
            return int(getattr(cur3, "rowcount", 0) or 0) == 1
        return False


def task_recheck_row(task_id: str):
    """补偿终检读原任务终态 (completed_at, agent_type, result_text)；无行返回 None。

    列序与 api/patrol_compensate.py 原查询逐字一致（调用方按下标解包）。
    """
    return read_one_any("task_recheck_row", (task_id,))


def event_created_ats_by_types(repo_key: str, issue_id: str, types) -> list[tuple]:
    """指定事件类型在该 issue 下的 created_at 列表（类型为 IN 列表，动态占位）。"""
    _types = tuple(types or ())
    if not _types:
        return []
    _ph = ",".join(["?"] * len(_types))
    with _any_cursor() as c:
        c.execute(f"SELECT created_at FROM events WHERE repo_key=? AND issue_id=? "
                  f"AND event_type IN ({_ph})", (repo_key, issue_id, *_types))
        return [tuple(r) for r in (c.fetchall() or [])]


def tasks_result_texts(task_ids, chunk: int = 500) -> dict:
    """按 id 分块回捞 result_text（单块失败不伤其它块：S5 容错口径）。"""
    out: dict = {}
    ids = list(task_ids or [])
    if not ids:
        return out
    with _any_cursor() as c:
        for i in range(0, len(ids), chunk):
            _ids = ids[i:i + chunk]
            _ph = ",".join(["?"] * len(_ids))
            try:
                c.execute(f"SELECT id, result_text FROM agent_tasks WHERE id IN ({_ph})",
                          tuple(_ids))
                for tid, rt in c.fetchall() or []:
                    out[tid] = rt or ""
            except Exception as exc:  # noqa: BLE001 - 单块失败吞掉，其余块照常
                log.warning(f"result_text batch fetch failed (chunk {i // chunk}): {exc}")
    return out


def tasks_completed_page(cutoff: str, last_id: str, chunk: int = 500) -> list[tuple]:
    """keyset 分页取候选任务（一页一查询；锁/连接不跨页存活）。"""
    with _any_cursor() as c:
        if cutoff:
            c.execute(_any_sql("tasks_completed_page_cutoff"), (cutoff, last_id, int(chunk)))
        else:
            c.execute(_any_sql("tasks_completed_page_all"), (last_id, int(chunk)))
        return [tuple(r) for r in (c.fetchall() or [])]


# ---------- 看板聚合（insight dashboard 只读；过滤条件由调用方给，SQL 留在本域）----------
#
# 这些是报表聚合而不是单表读写，所以给的是「聚合原语」：调用方传 repo_keys / org_id，
# 本域拼 WHERE 并聚合。跨域 JOIN（如 agent_tasks ⋈ issue）也在本域内完成，
# 避免 insight 侧持有其他域的表名。

def _dash_where(repo_keys=None, org_id=None, prefix: str = "WHERE 1=1") -> tuple[str, list]:
    """看板通用过滤：(SQL 片段, 参数)。repo_keys 走 IN，org_id 非空时加等值。"""
    parts = [prefix]
    params: list = []
    keys = [str(k) for k in (repo_keys or []) if k]
    if keys:
        parts.append(f"AND repo_key IN ({','.join(['?'] * len(keys))})")
        params += keys
    if org_id:
        parts.append("AND org_id=?")
        params.append(org_id)
    return " ".join(parts), params


def dashboard_task_status_counts(repo_keys=None, org_id=None) -> list[tuple]:
    """任务状态分布 [(status, count)]。"""
    where, params = _dash_where(repo_keys, org_id)
    with _any_cursor() as c:
        c.execute(f"SELECT status, COUNT(*) FROM agent_tasks {where} GROUP BY status", params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_issue_latest_status_counts(repo_keys=None, org_id=None) -> list[tuple]:
    """按 issue 最新任务状态聚合 [(status, count)]（一 issue 一任务口径）。"""
    where, params = _dash_where(repo_keys, org_id,
                                prefix="WHERE issue_id IS NOT NULL AND issue_id <> ''")
    with _any_cursor() as c:
        c.execute(
            "SELECT t.status, COUNT(*) FROM ("
            "SELECT issue_id, MAX(id) AS max_id FROM agent_tasks "
            f"{where} GROUP BY issue_id"
            ") latest JOIN agent_tasks t ON t.id = latest.max_id GROUP BY t.status", params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_stuck_tasks(cutoff_iso: str, repo_keys=None, org_id=None,
                          limit: int = 100) -> list[tuple]:
    """running 且 started_at 早于阈值的任务（按 started_at 升序取前 N）。"""
    where, params = _dash_where(repo_keys, org_id,
                                prefix="WHERE status='running' AND started_at IS NOT NULL "
                                       "AND started_at < ?")
    with _any_cursor() as c:
        c.execute(
            "SELECT id, agent_type, runtime_id, started_at, repo_key, issue_id, event_seq "
            f"FROM agent_tasks {where} ORDER BY started_at ASC LIMIT {int(limit)}",
            [cutoff_iso] + params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_failed_tasks(repo_keys=None, org_id=None) -> list[tuple]:
    """failed 任务明细（按 completed_at 倒序，调用方自行截断）。"""
    where, params = _dash_where(repo_keys, org_id, prefix="WHERE status='failed'")
    with _any_cursor() as c:
        c.execute(
            "SELECT id, agent_type, runtime_id, started_at, completed_at, repo_key, issue_id, "
            "event_seq, result_text, exit_code, errors "
            f"FROM agent_tasks {where} ORDER BY completed_at DESC", params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_task_by_source(repo_keys=None, org_id=None) -> list[tuple]:
    """未终结任务按来源分组 [(agent_type, status, count)]。"""
    where, params = _dash_where(repo_keys, org_id,
                                prefix="WHERE status IN ('queued','dispatched','running')")
    with _any_cursor() as c:
        c.execute(f"SELECT agent_type, status, COUNT(*) FROM agent_tasks {where} "
                  "GROUP BY agent_type, status", params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_today_status_counts(day_prefix: str, repo_keys=None, org_id=None) -> list[tuple]:
    """今日创建任务的状态分布（created_at ISO 日期前缀匹配）。"""
    where, params = _dash_where(repo_keys, org_id, prefix="WHERE created_at LIKE ?")
    with _any_cursor() as c:
        c.execute(f"SELECT status, COUNT(*) FROM agent_tasks {where} GROUP BY status",
                  [day_prefix + "%"] + params)
        return [tuple(r) for r in (c.fetchall() or [])]


_DASH_WORKER_COLS = (
    "SELECT id, name, device_name, status, last_seen_at, last_ip, cpu_percent, memory_percent, "
    "disk_percent, tmp_percent, bandwidth_percent, bandwidth_mbps, load_avg, owner_name "
    "FROM runtime_credentials WHERE status != 'revoked' "
    "AND last_seen_at IS NOT NULL AND last_seen_at >= ?")


def dashboard_worker_rows(stale_cutoff: str, org_id=None, uname: str = "",
                          repo_key: str = "", repo: str = "") -> list[tuple]:
    """看板 worker 行（三种可见性口径：组织隔离 / 仓库级 / 个人组织全局）。"""
    with _any_cursor() as c:
        if org_id:
            c.execute(_DASH_WORKER_COLS + " AND org_id=? ORDER BY id", (stale_cutoff, org_id))
        elif repo:
            c.execute(_DASH_WORKER_COLS +
                      " AND (scope='global' OR (scope='user' AND owner_name=?) "
                      "OR repo_key=? OR repo_key=?) ORDER BY id",
                      (stale_cutoff, uname, repo_key, repo))
        else:
            c.execute(_DASH_WORKER_COLS +
                      " AND (scope='global' OR (scope='user' AND owner_name=?)) ORDER BY id",
                      (stale_cutoff, uname))
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_local_runtimes(org_id=None) -> list[tuple]:
    """本机直连引擎（credential_id=0）行。"""
    sql = ("SELECT ar.id, ar.name, ar.status, ar.last_seen_at, ar.host_name, "
           "ar.healthy, ar.busy_slots, ar.max_concurrent FROM agent_runtimes ar "
           "WHERE ar.credential_id = 0")
    params: list = []
    if org_id:
        sql += " AND ar.org_id=?"
        params.append(org_id)
    with _any_cursor() as c:
        c.execute(sql, params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_running_counts(org_id=None) -> list[tuple]:
    """每个引擎当前 running 任务数 [(runtime_id, count)]。"""
    sql = "SELECT runtime_id, COUNT(*) FROM agent_tasks WHERE status='running'"
    params: list = []
    if org_id:
        sql += " AND org_id=?"
        params.append(org_id)
    sql += " GROUP BY runtime_id"
    with _any_cursor() as c:
        c.execute(sql, params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_avg_durations(org_id=None) -> list[tuple]:
    """每个引擎已完成任务的平均耗时（秒）；sqlite 用 strftime，MySQL 回落 TIMESTAMPDIFF。"""
    where = "WHERE status IN ('completed','failed') AND runtime_id > 0"
    params: list = []
    if org_id:
        where += " AND org_id=?"
        params.append(org_id)
    with _any_cursor() as c:
        try:
            c.execute(
                "SELECT runtime_id, AVG(CASE WHEN completed_at IS NOT NULL AND started_at IS NOT NULL "
                "THEN (strftime('%s', completed_at) - strftime('%s', started_at)) ELSE NULL END) "
                f"AS avg_sec FROM agent_tasks {where} GROUP BY runtime_id", params)
            return [tuple(r) for r in (c.fetchall() or [])]
        except Exception:  # noqa: BLE001 - MySQL 方言回落
            c.execute(
                "SELECT runtime_id, AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) AS avg_sec "
                f"FROM agent_tasks {where} AND started_at IS NOT NULL AND completed_at IS NOT NULL "
                "GROUP BY runtime_id", params)
            return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_runtime_rows(org_id=None) -> list[tuple]:
    """引擎行 + 绑定凭据（LEFT JOIN runtime_credentials，含探测字段与 org_id）。"""
    sql = ("SELECT ar.id, ar.name, ar.credential_id, ar.status, ar.last_seen_at, ar.provider, "
           "ar.healthy, ar.busy_slots, ar.max_concurrent, ar.version, rc.name AS rc_name, "
           "rc.device_name, rc.status AS cred_status, rc.id AS cred_pk, ar.host_name, "
           "ar.probe_status, ar.probe_latency, ar.probe_last, ar.probe_error, ar.org_id "
           "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id")
    params: list = []
    if org_id:
        sql += " WHERE ar.org_id=?"
        params.append(org_id)
    sql += " ORDER BY ar.id"
    with _any_cursor() as c:
        c.execute(sql, params)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_credential_scope_owner(cred_id) -> tuple | None:
    """凭据的 (scope, owner_name)。"""
    return read_one_any("runtime_credentials_scope_owner", (cred_id,))


def dashboard_credential_name_device(cred_id) -> tuple | None:
    """凭据的 (name, device_name)。"""
    return read_one_any("runtime_credentials_name_device", (cred_id,))


def dashboard_issue_contrib(repo_keys) -> list[tuple]:
    """需求协作统计 [(creator, assignee, user_id, count, completed)]（issue 单表，非子任务）。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(
            "SELECT COALESCE(NULLIF(created_by,''), '') AS creator, "
            "COALESCE(assignee,'') AS assignee, COALESCE(user_id,0), COUNT(*), "
            f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) FROM issue "
            f"WHERE repo_key IN ({ph}) AND is_subtask_mode=0 "
            "GROUP BY creator, assignee, COALESCE(user_id,0)", keys)
        return [tuple(r) for r in (c.fetchall() or [])]


def dashboard_task_issue_rows(task_ids, chunk: int = 500) -> list[tuple]:
    """token 归属链回链：[(task_id, created_by, assignee, user_id, is_subtask_mode, issue_id)]。

    agent_tasks ⋈ issue（都在本域），分块避免 SQLite 变量上限。
    """
    ids = [t for t in (task_ids or []) if t]
    out: list[tuple] = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        ph = ",".join(["?"] * len(part))
        with _any_cursor() as c:
            c.execute(
                "SELECT t.id, i.created_by, i.assignee, COALESCE(i.user_id, 0), "
                "i.is_subtask_mode, i.id FROM agent_tasks t LEFT JOIN issue i "
                "ON i.repo_key = t.repo_key AND i.issue_number = "
                f"CAST(SUBSTR(t.issue_id, 2) AS INTEGER) WHERE t.id IN ({ph})", part)
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


# ---------- 会话配置页（mgmt/routes/agents.py）组合原语 ----------

def agent_session_row(repo_keys, agent_type: str, token_field: str) -> tuple | None:
    """按仓库集合取某类 Agent 的会话行（token 列按 provider 能力位动态选）。

    动态列名只接受 `^[a-z_]+$`（与 managed_repos 补列同一道校验），列序固定：
    id, runtime_id, skill, skill_id, <token>, runtime_session_id, status,
    last_active_at, enable, subscribe_events。
    """
    import re as _re

    if not _re.match(r"^[a-z_]+$", token_field or ""):
        raise ValueError(f"invalid session token field: {token_field!r}")
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return None
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(
            "SELECT id, runtime_id, skill, skill_id, " + token_field + ", runtime_session_id, "
            "status, last_active_at, enable, subscribe_events FROM devops_agent_session "
            f"WHERE repo_key IN ({ph}) AND agent_type=?", (*keys, agent_type))
        return c.fetchone()


def event_status_counts(repo_keys, agent_type: str) -> list[tuple]:
    """某类 Agent 在仓库集合内的事件状态分布 [(status, count)]。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    ph = ",".join(["?"] * len(keys))
    with _any_cursor() as c:
        c.execute(f"SELECT status, COUNT(*) FROM events WHERE repo_key IN ({ph}) "
                  "AND agent_type=? GROUP BY status", (*keys, agent_type))
        return [tuple(r) for r in (c.fetchall() or [])]


_EVENT_SEARCH_COLS = (
    "seq, event_type, title, actor, summary, status, created_at, runtime, "
    "skill, exit_code, duration, started_at, completed_at, issue_id, repo_key")


def events_search(repo_keys, agent_type: str, keyword: str = "", page: int = 1,
                  page_size: int = 10) -> tuple[int, list[tuple]]:
    """事件历史搜索（keyword 命中 seq/issue_id/event_type/title/summary/actor）。

    返回 (total, rows)。LIKE 条件在 owner 域构造，调用方只给关键词。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return 0, []
    ph = ",".join(["?"] * len(keys))
    where = [f"repo_key IN ({ph})", "agent_type=?"]
    params: list = list(keys) + [agent_type]
    if keyword:
        kw = f"%{keyword}%"
        where.append("(CAST(seq AS CHAR) LIKE ? OR issue_id LIKE ? OR event_type LIKE ? "
                     "OR title LIKE ? OR summary LIKE ? OR actor LIKE ?)")
        params += [kw] * 6
    where_sql = " AND ".join(where)
    offset = max(0, (int(page) - 1) * int(page_size))
    with _any_cursor() as c:
        c.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params)
        total = int((c.fetchone() or [0])[0] or 0)
        c.execute(f"SELECT {_EVENT_SEARCH_COLS} FROM events WHERE {where_sql} "
                  "ORDER BY seq DESC LIMIT ? OFFSET ?", params + [int(page_size), offset])
        return total, [tuple(r) for r in (c.fetchall() or [])]


def vod_conversations_delete(agent_session_ids=None) -> int:
    """按会话 id 清 VOD 会话行（可批量）。"""
    ids = [str(i) for i in (agent_session_ids or []) if i]
    if not ids:
        return 0
    ph = ",".join(["?"] * len(ids))
    with _any_cursor(write=True) as c:
        cur = c.execute(f"DELETE FROM vod_conversations WHERE agent_session_id IN ({ph})", ids)
        return int(getattr(cur, "rowcount", 0) or 0)


def vod_conversations_delete_by_repo(repo_key: str) -> int:
    """清某仓库所有 VOD 会话行（按该仓库的 vod 会话子查询，两表都在本域）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "DELETE FROM vod_conversations WHERE agent_session_id IN "
            "(SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod')",
            (repo_key,))
        return int(getattr(cur, "rowcount", 0) or 0)


def skill_id_by_name_scope(name: str, scope: str, owners=None) -> int:
    """同名技能的 id（scope 精确；owners 非空时限定 owner 集合，回退 0）。"""
    if not name or not scope:
        return 0
    _owners = [str(o) for o in (owners or []) if o]
    with _any_cursor() as c:
        if _owners:
            ph = ",".join(["?"] * len(_owners))
            c.execute(f"SELECT id FROM skill WHERE skill_name=? AND scope=? "
                      f"AND owner_id IN ({ph}) AND is_active=1 LIMIT 1",
                      (name, scope, *_owners))
        else:
            c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1",
                      (name, scope))
        row = c.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def scene_detach_log_rebound(scene_row_id, node_tag: str, rebound_at: str) -> int:
    """把场景解绑提示标记为已回绑（仅未标记的行）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(
            "UPDATE scene_engine_detach_log SET rebound_at=? WHERE scene_row_id=? AND node_tag=? "
            "AND (rebound_at='' OR rebound_at IS NULL)", (rebound_at, scene_row_id, node_tag))
        return int(getattr(cur, "rowcount", 0) or 0)


# ---------- 技能页（mgmt/routes/skills.py）查询原语 ----------
#
# skill 归运行域，列表/统计的可见性口径（scope 组合、owner、仓库、组织）跟随表一起放这里；
# 调用方用结构化参数表达「看谁的范围」，不再拼 WHERE 片段。

_SKILL_LIST_COLS = (
    "id, skill_name, display_name, description, agent_type, scope, owner_id, "
    "is_featured, like_count, sub_scene, category_id")


def _skill_scope_conditions(scope=None, user_id="", repo_key="", org_id="") -> tuple[list, list]:
    """可见范围条件（与原 mgmt 侧分支逐条对应）。"""
    conds: list = ["is_active=1"]
    params: list = []
    if scope == "all":
        pass  # 不加 scope 条件（官方 + 个人 + 所有仓库）
    elif scope == "official":
        conds.append("scope='official'")
    elif scope == "repo":
        conds.append("scope='repo'")
        conds.append("owner_id=?")
        params.append(repo_key)
    elif scope == "user":
        conds.append("scope='user'")
        conds.append("owner_id=?")
        params.append(str(user_id))
    elif repo_key:
        conds.append("(scope='official' OR (scope='user' AND owner_id=?) "
                     "OR (scope='repo' AND owner_id=?))")
        params.extend([str(user_id), repo_key])
    else:
        conds.append("(scope='official' OR (scope='user' AND owner_id=?))")
        params.append(str(user_id))
    # 组织隔离：official 对全组织可见；repo/user 的可见性靠 owner，不追加 org_id
    if org_id and scope not in ("repo", "user"):
        conds.append("(org_id='' OR org_id=?)")
        params.append(org_id)
    return conds, params


def skill_search(scope=None, user_id="", repo_key="", org_id="", q: str = "",
                 agent_type: str = "", category_id: int = 0, sort: str = "",
                 page: int = 1, page_size: int = 0) -> tuple[int, list[tuple]]:
    """技能列表查询：返回 (total, rows)。page_size<=0 表示不分页（total 为 0）。"""
    conds, params = _skill_scope_conditions(scope, user_id, repo_key, org_id)
    kw = (q or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append("(skill_name LIKE ? OR display_name LIKE ? OR description LIKE ?)")
        params.extend([like, like, like])
    at = (agent_type or "").strip()
    if at:
        conds.append("agent_type=?")
        params.append(at)
    if category_id and int(category_id) > 0:
        conds.append("category_id=?")
        params.append(int(category_id))

    where_sql = " WHERE " + " AND ".join(conds)
    order_by = ("ORDER BY updated_at DESC" if sort == "latest"
                else "ORDER BY like_count DESC" if sort == "hot"
                else "ORDER BY agent_type, skill_name")
    with _any_cursor() as c:
        if page_size and int(page_size) > 0:
            c.execute(f"SELECT COUNT(*) FROM skill{where_sql}", params)
            total = int((c.fetchone() or [0])[0] or 0)
            offset = max(0, (int(page) - 1) * int(page_size))
            c.execute(f"SELECT {_SKILL_LIST_COLS} FROM skill{where_sql} {order_by} "
                      f"LIMIT {int(page_size)} OFFSET {offset}", params)
            return total, [tuple(r) for r in (c.fetchall() or [])]
        c.execute(f"SELECT {_SKILL_LIST_COLS} FROM skill{where_sql} {order_by}", params)
        return 0, [tuple(r) for r in (c.fetchall() or [])]


def skill_agent_counts(scope=None, user_id="", repo_key="", org_id="") -> list[tuple]:
    """按 agent_type 的技能计数（顺序固定 vod/issue/coding/qa/sre/chat/其它）。"""
    conds, params = _skill_scope_conditions(scope, user_id, repo_key, org_id)
    where_sql = " WHERE " + " AND ".join(conds)
    with _any_cursor() as c:
        c.execute(
            f"SELECT agent_type, COUNT(*) FROM skill{where_sql} GROUP BY agent_type ORDER BY "
            "CASE agent_type WHEN 'vod' THEN 1 WHEN 'issue' THEN 2 WHEN 'coding' THEN 3 "
            "WHEN 'qa' THEN 4 WHEN 'sre' THEN 5 WHEN 'chat' THEN 6 ELSE 99 END", params)
        return [tuple(r) for r in (c.fetchall() or [])]


def skill_download_row(skill_id, user_id="", repo_key: str = "") -> tuple | None:
    """下载/详情用的技能行 (id, skill_path, scope, owner_id, skill_name)，含可见性校验。"""
    with _any_cursor() as c:
        if repo_key:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? "
                "AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) "
                "OR (scope='repo' AND owner_id=?))",
                (int(skill_id), str(user_id), repo_key))
        else:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? "
                "AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) "
                "OR scope='repo')", (int(skill_id), str(user_id)))
        return c.fetchone()


def skill_repo_id_by_name(name: str, owner_id: str) -> int:
    """repo scope 同名技能 id（不存在返回 0）。"""
    with _any_cursor() as c:
        c.execute("SELECT id FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? "
                  "AND is_active=1 LIMIT 1", (name, owner_id))
        row = c.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def skill_repo_count_by_name(name: str, owner_id: str) -> int:
    """repo scope 同名技能条数（用于「已存在」判定）。"""
    with _any_cursor() as c:
        c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? "
                  "AND is_active=1", (name, owner_id))
        return int((c.fetchone() or [0])[0] or 0)


def skill_delete_by_id(skill_id) -> int:
    """物理删除技能行（按 id；软删由 is_active 控制，此处与原实现一致）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("DELETE FROM skill WHERE id=?", (int(skill_id),))
        return int(getattr(cur, "rowcount", 0) or 0)


def skill_exists_active(skill_id) -> bool:
    """技能是否存在且未停用。"""
    return read_one_any("skill_exists_active", (int(skill_id),)) is not None


# ---------- 仓库改名迁移（runtime 段：repo_key 跟随表 + skill 归属 + meta 前缀键） ----------
#
# 原实现在 kernel/repo_migrate.py 里用一个事务改三域的表；三库独立后跨库事务不可得，
# 按域拆成两段（mgmt 段在 mgmt public）。两段都是「删旧留新」语义，重复执行即幂等，
# 第二段失败时第一段已提交 —— 调用方重试即可收敛。

#: 以 repo_key 为索引的 runtime 表（固定白名单，不接受调用方传表名）
REPO_KEY_TABLES = (
    "issue", "issue_state", "issue_agent_session", "events", "event_skipped",
    "event_thread", "agent_tasks", "chat_conversations", "chat_agent_session",
    "devops_agent_session", "repo_im_group_binding",
)


def _rt_existing_tables(c) -> set:
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        try:
            return {list(r)[-1] for r in c.execute("SHOW TABLES")}
        except Exception:
            return set()


def _rt_has_repo_key(c, table: str) -> bool:
    try:
        return "repo_key" in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return False


def _rt_unique_repo_key(c, table: str) -> bool:
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


def migrate_repo_key_runtime(old_keys, new: str) -> dict[str, int]:
    """同一事务内改写本域的 repo_key 关联数据；返回 {表名: 改写行数, "skill"/"meta": 行数}。"""
    keys = [str(k) for k in (old_keys or []) if k]
    out: dict[str, int] = {}
    if not keys:
        return out
    ph = ",".join(["?"] * len(keys))
    with _any_tx() as c:
        existing = _rt_existing_tables(c)
        # 1) repo_key 列跟随（UNIQUE(repo_key) 冲突时删源行，保留目标行）
        for tbl in REPO_KEY_TABLES:
            if tbl not in existing or not _rt_has_repo_key(c, tbl):
                continue
            n = 0
            if _rt_unique_repo_key(c, tbl):
                c.execute(f"SELECT repo_key FROM {tbl} WHERE repo_key IN ({ph})", keys)
                for (rk,) in (c.fetchall() or []):
                    if c.execute(f"SELECT 1 FROM {tbl} WHERE repo_key=? LIMIT 1", (new,)).fetchone():
                        cur = c.execute(f"DELETE FROM {tbl} WHERE repo_key=?", (rk,))
                    else:
                        cur = c.execute(f"UPDATE {tbl} SET repo_key=? WHERE repo_key=?", (new, rk))
                    n += int(getattr(cur, "rowcount", 0) or 0)
            else:
                cur = c.execute(f"UPDATE {tbl} SET repo_key=? WHERE repo_key IN ({ph})",
                                [new] + keys)
                n = int(getattr(cur, "rowcount", 0) or 0)
            if n:
                out[tbl] = n

        # 2) skill(scope='repo')：owner_id 存 repo_key，UNIQUE(skill_name, scope, owner_id)
        if "skill" in existing:
            n = 0
            c.execute(f"SELECT id, skill_name FROM skill WHERE scope='repo' AND owner_id IN ({ph})",
                      keys)
            for sid, skill_name in (c.fetchall() or []):
                conflict = c.execute(
                    "SELECT 1 FROM skill WHERE scope='repo' AND owner_id=? AND skill_name=? LIMIT 1",
                    (new, skill_name)).fetchone()
                if conflict:
                    cur = c.execute("DELETE FROM skill WHERE id=?", (sid,))
                else:
                    cur = c.execute("UPDATE skill SET owner_id=? WHERE id=?", (new, sid))
                n += int(getattr(cur, "rowcount", 0) or 0)
            if n:
                out["skill"] = n

        # 3) meta：onboarded:* / cleanup_pending:*
        if "meta" in existing:
            n = 0
            for prefix in ("onboarded:", "cleanup_pending:"):
                for old in keys:
                    old_k, new_k = f"{prefix}{old}", f"{prefix}{new}"
                    if c.execute("SELECT 1 FROM meta WHERE `key`=? LIMIT 1", (new_k,)).fetchone():
                        cur = c.execute("DELETE FROM meta WHERE `key`=?", (old_k,))
                    else:
                        cur = c.execute("UPDATE meta SET `key`=? WHERE `key`=?", (new_k, old_k))
                    n += int(getattr(cur, "rowcount", 0) or 0)
            if n:
                out["meta"] = n
    return out


# ---------- 仓库级联清理（runtime 段） ----------
#
# 仓库删除时的级联清理：子表先删（无 repo_key 列，经父表 id 关联）、再直删本域 repo_key 表、
# 再删 repo scope 技能与 onboarded 幂等键。全部在本域一个事务内完成（跨库无法同事务）。

def purge_repo_rows(keys, dry_run: bool = False) -> dict[str, int]:
    """删除/统计仓库在 runtime 域的数据；返回 {表名或子表标签: 行数}。"""
    _keys = [str(k) for k in (keys or []) if k]
    stats: dict[str, int] = {}
    if not _keys:
        return stats
    ph = ",".join(["?"] * len(_keys))
    with _any_tx() as c:
        existing = _rt_existing_tables(c)
        if dry_run:
            for tbl in REPO_KEY_TABLES:
                if tbl in existing:
                    c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE repo_key IN ({ph})", _keys)
                    stats[tbl] = int((c.fetchone() or [0])[0] or 0)
            return stats

        # 1) 依赖子表（经父表 id 关联）
        if "agent_tasks" in existing and "task_messages" in existing:
            c.execute(f"SELECT id FROM agent_tasks WHERE repo_key IN ({ph})", _keys)
            ids = [r[0] for r in (c.fetchall() or [])]
            if ids:
                _p = ",".join(["?"] * len(ids))
                cur = c.execute(f"DELETE FROM task_messages WHERE task_id IN ({_p})", ids)
                stats["task_messages"] = int(getattr(cur, "rowcount", 0) or 0)
        if "devops_agent_session" in existing and "vod_conversations" in existing:
            cur = c.execute(
                "DELETE FROM vod_conversations WHERE agent_session_id IN "
                f"(SELECT id FROM devops_agent_session WHERE repo_key IN ({ph}))", _keys)
            stats["vod_conversations"] = int(getattr(cur, "rowcount", 0) or 0)
        if "chat_conversations" in existing and "chat_messages" in existing:
            c.execute(f"SELECT id FROM chat_conversations WHERE repo_key IN ({ph})", _keys)
            ids = [r[0] for r in (c.fetchall() or [])]
            if ids:
                _p = ",".join(["?"] * len(ids))
                cur = c.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({_p})", ids)
                stats["chat_messages"] = int(getattr(cur, "rowcount", 0) or 0)

        # 2) 本域 repo_key 表直删
        for tbl in REPO_KEY_TABLES:
            if tbl not in existing:
                continue
            cur = c.execute(f"DELETE FROM {tbl} WHERE repo_key IN ({ph})", _keys)
            stats[tbl] = int(getattr(cur, "rowcount", 0) or 0)

        # 3) repo scope 技能（owner_id 存 repo_key）
        if "skill" in existing:
            cur = c.execute(f"DELETE FROM skill WHERE scope='repo' AND owner_id IN ({ph})", _keys)
            stats["skill"] = int(getattr(cur, "rowcount", 0) or 0)

        # 4) onboarding 幂等标记（重建同名仓库应重新引导）
        if "meta" in existing:
            ob_keys = [f"onboarded:{k}" for k in _keys]
            _p = ",".join(["?"] * len(ob_keys))
            cur = c.execute(f"DELETE FROM meta WHERE `key` IN ({_p})", ob_keys)
            stats["meta_onboarded"] = int(getattr(cur, "rowcount", 0) or 0)
    return stats


def meta_set(key: str, value: str) -> int:
    """写入 meta 键（INSERT OR REPLACE 语义，幂等）。"""
    with _any_cursor(write=True) as c:
        cur = c.execute("INSERT OR REPLACE INTO meta (`key`, value) VALUES (?, ?)", (key, value))
        return int(getattr(cur, "rowcount", 0) or 0)


def meta_delete_keys(keys) -> int:
    """删除给定 meta 键；返回删除行数。"""
    _keys = [str(k) for k in (keys or []) if k]
    if not _keys:
        return 0
    ph = ",".join(["?"] * len(_keys))
    with _any_cursor(write=True) as c:
        cur = c.execute(f"DELETE FROM meta WHERE `key` IN ({ph})", _keys)
        return int(getattr(cur, "rowcount", 0) or 0)


def meta_keys_like(prefix: str) -> list[str]:
    """按前缀取 meta 键（`LIKE 'prefix%'`）。"""
    with _any_cursor() as c:
        c.execute("SELECT `key` FROM meta WHERE `key` LIKE ?", (f"{prefix}%",))
        return [r[0] for r in (c.fetchall() or [])]


# ---------- 告警求值聚合（insight/observability/alerts 只读；SQL 留本域） ----------

def alerts_task_inflight(stuck_bound: str) -> list[tuple]:
    """按仓库的排队数 + 卡住数 [(repo_key, queued, stuck)]。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT repo_key,"
            " SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status IN ('dispatched','running')"
            "          AND started_at IS NOT NULL AND started_at != '' AND started_at < ?"
            "     THEN 1 ELSE 0 END)"
            " FROM agent_tasks WHERE repo_key IS NOT NULL AND repo_key != ''"
            "   AND (status='queued' OR status IN ('dispatched','running'))"
            " GROUP BY repo_key", (stuck_bound,))
        return [tuple(r) for r in (c.fetchall() or [])]


def alerts_task_terminal(fail_cutoff: str) -> list[tuple]:
    """按仓库×状态统计窗口内终态任务 [(repo_key, status, count)]。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT repo_key, status, COUNT(*) FROM agent_tasks"
            " WHERE repo_key IS NOT NULL AND repo_key != ''"
            "   AND status IN ('completed','failed','cancelled')"
            "   AND completed_at IS NOT NULL AND completed_at != '' AND completed_at >= ?"
            " GROUP BY repo_key, status", (fail_cutoff,))
        return [tuple(r) for r in (c.fetchall() or [])]


def alerts_task_timeout(fail_cutoff: str, signature: str) -> list[tuple]:
    """窗口内「超时失败」按仓库计数 [(repo_key, count)]（按 result_text/errors 签名匹配）。"""
    like = f"%{signature}%"
    with _any_cursor() as c:
        c.execute(
            "SELECT repo_key, COUNT(*) FROM agent_tasks"
            " WHERE repo_key IS NOT NULL AND repo_key != ''"
            "   AND status='failed'"
            "   AND completed_at IS NOT NULL AND completed_at != '' AND completed_at >= ?"
            "   AND (result_text LIKE ? OR errors LIKE ?)"
            " GROUP BY repo_key", (fail_cutoff, like, like))
        return [tuple(r) for r in (c.fetchall() or [])]


def alerts_task_mutation(mut0_cutoff: str, mut1_cutoff: str) -> list[tuple]:
    """突变检测两窗口 [(repo_key, status, 本窗口数, 上一窗口数)]。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT repo_key, status,"
            " SUM(CASE WHEN completed_at > ? THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN completed_at > ? AND completed_at < ? THEN 1 ELSE 0 END)"
            " FROM agent_tasks WHERE repo_key IS NOT NULL AND repo_key != ''"
            "   AND status IN ('completed','failed','cancelled')"
            "   AND completed_at IS NOT NULL AND completed_at != '' AND completed_at >= ?"
            " GROUP BY repo_key, status",
            (mut0_cutoff, mut1_cutoff, mut0_cutoff, mut1_cutoff))
        return [tuple(r) for r in (c.fetchall() or [])]


def alerts_task_rows_total() -> int:
    """agent_tasks 总行数（体量类告警的求值输入）。"""
    with _any_cursor() as c:
        c.execute("SELECT COUNT(*) FROM agent_tasks")
        return int((c.fetchone() or [0])[0] or 0)


def alerts_credential_heartbeats() -> list[tuple]:
    """未吊销凭据的 (status, last_seen_at) 清单（在线率求值用）。"""
    with _any_cursor() as c:
        c.execute("SELECT status, last_seen_at FROM runtime_credentials "
                  "WHERE status != 'revoked'")
        return [tuple(r) for r in (c.fetchall() or [])]


def credential_ids_visible(org_id: str = "", personal_org: bool = True,
                           uname: str = "", repo_keys=None) -> list[int]:
    """可见凭据 id（三口径：非个人组织按 org_id / 指定仓库 / 个人全局）。

    与 dashboard 的 worker 可见性同一套语义，条件在本域拼；调用方只传口径参数。
    """
    sql = "SELECT id FROM runtime_credentials WHERE status != 'revoked'"
    params: list = []
    keys = [str(k) for k in (repo_keys or []) if k]
    if org_id and not personal_org:
        sql += " AND org_id=?"
        params.append(org_id)
    elif keys:
        ph = ",".join(["?"] * len(keys))
        sql += f" AND (scope='global' OR (scope='user' AND owner_name=?) OR repo_key IN ({ph}))"
        params += [uname, *keys]
    else:
        sql += " AND (scope='global' OR (scope='user' AND owner_name=?))"
        params.append(uname)
    with _any_cursor() as c:
        c.execute(sql, params)
        return [int(r[0]) for r in (c.fetchall() or [])]


# ---------- 场景页（mgmt/routes/scenes.py）读路径：技能/引擎/会话 ----------

def task_issue_repo(task_id: str):
    """任务所属 (issue_id, repo_key)；无行返回 None。"""
    with _any_cursor() as c:
        c.execute("SELECT issue_id, repo_key FROM agent_tasks WHERE id=? LIMIT 1", (task_id,))
        return c.fetchone()


def skills_by_ids(skill_ids) -> list[tuple]:
    """按 id 批量取技能（id, name, display, agent_type, description, scope），只取启用行。"""
    ids = [int(i) for i in (skill_ids or []) if str(i).strip()]
    if not ids:
        return []
    ph = ",".join(["?"] * len(ids))
    with _any_cursor() as c:
        c.execute(f"SELECT id, skill_name, display_name, agent_type, description, scope "
                  f"FROM skill WHERE id IN ({ph}) AND is_active=1", ids)
        return [tuple(r) for r in (c.fetchall() or [])]


def skill_ids_active(skill_ids) -> list[int]:
    """批量校验技能 id 仍启用；返回命中的 id 列表。"""
    ids = [int(i) for i in (skill_ids or []) if str(i).strip()]
    if not ids:
        return []
    ph = ",".join(["?"] * len(ids))
    with _any_cursor() as c:
        c.execute(f"SELECT id FROM skill WHERE id IN ({ph}) AND is_active=1", ids)
        return [int(r[0]) for r in (c.fetchall() or [])]


def scene_skill_search(user_id: str, repo_key: str, q: str = "", scope: str = "",
                       page: int = 1, page_size: int = 50) -> tuple:
    """场景页技能下拉：可见范围 = 官方 + 本人 + 该仓，可按关键词/范围过滤，返回 (total, rows)。

    条件在本域拼（skill 归运行域），调用方只给口径参数。
    """
    where = ["(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))"]
    params: list = [user_id, repo_key]
    if q:
        where.append("(skill_name LIKE ? OR display_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if scope:
        where.append("scope=?")
        params.append(scope)
    where_sql = " AND ".join(where)
    with _any_cursor() as c:
        c.execute(f"SELECT COUNT(*) FROM skill WHERE {where_sql}", params)
        total = int((c.fetchone() or [0])[0] or 0)
        offset = max(0, (int(page) - 1) * int(page_size))
        c.execute(
            "SELECT id, skill_name, display_name, scope, owner_id, description, agent_type "
            f"FROM skill WHERE {where_sql} ORDER BY scope, skill_name LIMIT ? OFFSET ?",
            params + [int(page_size), offset])
        return total, [tuple(r) for r in (c.fetchall() or [])]


def runtime_with_credential(runtime_id) -> tuple | None:
    """引擎行 + 其凭据的 (credential_id, credential_scope, credential_owner_id)。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT r.credential_id, rc.scope, rc.owner_id FROM agent_runtimes r "
            "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=?",
            (runtime_id,))
        return c.fetchone()


def runtime_capability_match(runtime_id, tag: str) -> bool:
    """引擎是否具备某能力标签（tag='all' 视为通配）。"""
    with _any_cursor() as c:
        c.execute("SELECT 1 FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                  "AND (tag=? OR tag='all') LIMIT 1", (runtime_id, tag))
        return c.fetchone() is not None


def runtime_status_healthy(runtime_id) -> tuple | None:
    """引擎的 (status, healthy) 位；无行返回 None。"""
    with _any_cursor() as c:
        c.execute("SELECT status, healthy FROM agent_runtimes WHERE id=?", (runtime_id,))
        return c.fetchone()


def devops_session_runtime_id(repo_key: str, agent_type: str) -> str:
    """仓库×代理类型绑定的引擎 id（无则空串）。"""
    with _any_cursor() as c:
        c.execute("SELECT runtime_id FROM devops_agent_session "
                  "WHERE repo_key=? AND agent_type=?", (repo_key, agent_type))
        row = c.fetchone()
        return str(row[0]) if row and row[0] else ""


# ── 分布式锁协议（`distributed_locks`，本域基表）──────────────────────────────
# 归属：`docs/weekly-11/table-owner.tsv` → grape_runtime_db.distributed_locks。
# 调用方是 `src/kernel/lock.py`（任何进程都会抢锁：mgmt/insight/runtime 各自后台线程）。
# 三库独立后 kernel 进程库里没有这张表（视图已退役 → 1146），因此协议整体上移到
# owner 域，kernel 只保留 holder 标识与时间戳计算。
#
# 语义保持（与原 kernel 实现逐条对应，不"顺手修好"）：
# - 抢锁 = INSERT 冲突 → 查过期 → `WHERE expires_at < now` 条件 UPDATE（DB 语句即原子判据）；
# - 三步在**同一事务**内（`_any_tx`），等价于原实现整段持 `db_lock` 的单进程原子性；
#   自有 DSN 下是真事务，单库/测试拓扑回落进程库 + db_lock；
# - INSERT 主键冲突被吞掉后继续（语句级回滚，SQLite/MySQL 事务均可继续），
#   与原实现的 `except: db.rollback()` 后继续查行一致；
# - 释放只删自己的 holder；续期后 SELECT 验 holder（不依赖 rowcount，pymysql 不可靠）；
# - 清理只删「过期且非本实例 holder」的行。


def acquire_distributed_lock(lock_key: str, holder: str, ttl: int, now: int) -> bool:
    """抢 per-key 分布式锁（任意拓扑）。`now` 由调用方取一次（秒），保证同一次调用内时间一致。"""
    params = (lock_key, holder, now, now + ttl, now, now)
    with _any_tx() as c:
        try:
            c.execute(_any_sql("lock_insert"), params)
        except Exception as exc:  # noqa: BLE001 - 主键冲突=已有锁，属协议分支
            log.debug(f"distributed lock insert conflict for {lock_key}: {exc}")
        else:
            return True

        row = c.execute(_any_sql("lock_row"), (lock_key,)).fetchone()
        if not row:
            # 冲突后行不见了（被其他进程删了）→ 重试 INSERT
            try:
                c.execute(_any_sql("lock_insert"), params)
            except Exception as exc:  # noqa: BLE001 - 同上，重试仍冲突即抢锁失败
                log.debug(f"distributed lock insert retry conflict for {lock_key}: {exc}")
                return False
            return True

        if int(row[1] or 0) < now:
            c.execute(_any_sql("lock_takeover"), (holder, now, now + ttl, now, lock_key, now))
            row2 = c.execute(_any_sql("lock_holder"), (lock_key,)).fetchone()
            return bool(row2 and row2[0] == holder)
        return False


def release_distributed_lock(lock_key: str, holder: str) -> int:
    """释放锁（只删自己 holder 的行），返回删除行数。"""
    return exec_any("lock_release", (lock_key, holder))


def renew_distributed_lock(lock_key: str, holder: str, ttl: int, now: int) -> bool:
    """续期锁（只续自己的）：UPDATE 后 SELECT 验 holder，返回是否仍持有。"""
    with _any_tx() as c:
        c.execute(_any_sql("lock_renew"), (now + ttl, now, lock_key, holder))
        row = c.execute(_any_sql("lock_holder"), (lock_key,)).fetchone()
        return bool(row and row[0] == holder)


def cleanup_expired_distributed_locks(now: int, holder: str) -> int:
    """清理过期且非本实例持有的锁，返回删除行数（防表膨胀兜底）。"""
    return exec_any("lock_cleanup", (now, holder))


# ── 指标桶取数（insight/observability/metric_rollup 跨域收口）─────────────────────
# 归属：`docs/weekly-11/table-owner.tsv` → grape_runtime_db.{agent_tasks,events,
# runtime_credentials}。原实现在 insight 侧持进程主库游标直查这三张表：单库拓扑能跑，
# 三库拓扑下 insight 进程库没有这些表（视图已退役 → 1146），被上层 `except` 吞成
# 「空桶 / 在线数恒 0」——聚合看板静默缺数据。取数上移本域后，SQL 只留这一份。
#
# 语义保持：列序、谓词、分组与原 SQL 逐字一致；参数一律由调用方传时间串/签名，
# 不接受任何 SQL 片段。时间窗半开（>= start, < end）照抄，不改口径。


def task_status_counts_by_created_any(start_iso: str, end_iso: str) -> list[tuple]:
    """桶内新建任务按 (repo_key, status) 计数：`[(repo_key, status, n), ...]`。"""
    return read_all_any("rollup_task_counts_by_created", (start_iso, end_iso))


def task_status_counts_by_completed_any(start_iso: str, end_iso: str) -> list[tuple]:
    """桶内终态任务按 (repo_key, status) 计数：`[(repo_key, status, n), ...]`。"""
    return read_all_any("rollup_task_counts_by_completed", (start_iso, end_iso))


def task_timeout_counts_by_repo_any(start_iso: str, end_iso: str, signature: str) -> list[tuple]:
    """桶内超时失败任务按仓库计数：`[(repo_key, n), ...]`。

    `signature` 是超时失败的特征串（如 `timed out after`），LIKE 通配符由本域拼装
    （`%sig%`，两侧都要求包含），调用方不构造 SQL 片段。空签名 → 空结果（`LIKE '%%'`
    会匹配全表，等于是把「超时失败」错算成「全部失败」，宁可不出数）。
    """
    sig = (signature or "").strip()
    if not sig:
        return []
    like = f"%{sig}%"
    return read_all_any("rollup_task_timeout_counts", (start_iso, end_iso, like, like))


def task_timing_rows_any(start_iso: str, end_iso: str) -> list[tuple]:
    """桶内已启动任务的原始时间列：`[(repo_key, created_at, started_at, completed_at), ...]`。

    排队等待/耗时的差值计算与「时间戳可解析性」判定留在调用方（那是分析口径，
    不是域数据口径）；本域只负责按 started_at 半开窗口取行。
    """
    return read_all_any("rollup_task_timing_rows", (start_iso, end_iso))


def _table_has_column(c, table: str, column: str) -> bool:
    """后端无关的列存在探测：`SELECT * ... LIMIT 0` 读 description。

    不能用 PRAGMA：`db_registry` 的薄兼容游标只对进程主库翻译 PRAGMA（MySQL 上直接
    1064），三库拓扑的产物探测必须两条后端同一写法。`table` 只接受本模块常量。
    """
    cur = c.execute(f"SELECT * FROM {table} LIMIT 0")
    return column in [str(d[0]) for d in (cur.description or [])]


def event_cascade_counts_by_repo_any(start_iso: str, end_iso: str) -> list[tuple]:
    """桶内事件回报丢失补偿数按仓库计数：`[(repo_key, n), ...]`。

    老库可能还没迁出 `events.report_missing` 列（Issue #1938 前）：列不存在时返回空
    列表（与原实现「探测不到列就跳过」同口径），探测在**本域连接**上做，不在调用方。
    """
    with _any_cursor() as c:
        if not _table_has_column(c, "events", "report_missing"):
            log.debug("events.report_missing absent; cascade compensation metric skipped")
            return []
        c.execute(_any_sql("rollup_event_cascade_counts"), (start_iso, end_iso))
        return list(c.fetchall() or [])


def online_credential_count_any(since_iso: str) -> int:
    """在线凭据数：未吊销、有最后心跳且不早于 `since_iso` 的凭据行数。"""
    row = read_one_any("rollup_online_credentials", (since_iso,))
    return int((row or [0])[0] or 0)
