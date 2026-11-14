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
    from datetime import datetime, timedelta, timezone

    lib = _mgmt_channel_lib()
    if lib is not None:
        try:
            return bool(lib.acquire_channel_lease(lease_key, token, owner, ttl_seconds))
        except Exception as exc:  # noqa: BLE001 - owner 域不可用时按原路径兜底
            log.warning(f"mgmt lib acquire_channel_lease failed, fallback to local SQL: {exc}")
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
    lib = _mgmt_channel_lib()
    if lib is not None:
        try:
            return bool(lib.release_channel_lease(lease_key, token))
        except Exception as exc:  # noqa: BLE001
            log.warning(f"mgmt lib release_channel_lease failed, fallback to local SQL: {exc}")
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

    lib = _mgmt_channel_lib()
    if lib is not None:
        try:
            return bool(lib.renew_channel_lease(lease_key, token, owner, ttl_seconds,
                                                expected_token))
        except Exception as exc:  # noqa: BLE001
            log.warning(f"mgmt lib renew_channel_lease failed, fallback to local SQL: {exc}")
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
    lib = _mgmt_channel_lib()
    if lib is not None:
        return int(lib.add_event_action_filter(fields, created_at))
    cols = ["event_type", "action", "allow", "actor", "require_changes", "block_changes",
            "handle_side_effect", "priority", "enabled", "created_at", "updated_at"]
    vals = [fields.get("event_type", ""), fields.get("action", ""),
            int(fields.get("allow", 0)), fields.get("actor", "any"),
            fields.get("require_changes", ""), fields.get("block_changes", ""),
            fields.get("handle_side_effect", ""), int(fields.get("priority", 100)),
            int(fields.get("enabled", 1)), created_at, created_at]
    ph = ", ".join(["%s"] * len(cols))
    with db_registry.cursor("runtime") as c:
        c.execute(f"INSERT INTO event_action_filter ({', '.join(cols)}) VALUES ({ph})", tuple(vals))
        return int(c.lastrowid or 0)


def update_event_action_filter(rule_id: int, fields: dict[str, Any], updated_at: str) -> int:
    """按白名单列更新事件过滤规则；返回受影响行数（0 = 不存在，供 404 判定）。

    W2：表归 mgmt 域，lib 可用时委托 mgmt public 写函数。
    """
    lib = _mgmt_channel_lib()
    if lib is not None:
        return int(lib.update_event_action_filter(rule_id, fields, updated_at))
    unknown = [k for k in fields if k not in _ACTION_FILTER_WRITABLE]
    if unknown:
        raise ValueError(f"column(s) {unknown} not writable through runtime lib")
    cols = [k for k in fields if k in _ACTION_FILTER_WRITABLE]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=%s"
    params = tuple(fields[c] for c in cols) + (updated_at, int(rule_id))
    with db_registry.cursor("runtime") as c:
        c.execute(f"UPDATE event_action_filter SET {sets} WHERE id=%s", params)
        return int(c.rowcount or 0)


def delete_event_action_filter(rule_id: int) -> int:
    """删除规则（W2：表归 mgmt，lib 可用时委托 mgmt public 写函数）。"""
    lib = _mgmt_channel_lib()
    if lib is not None:
        return int(lib.delete_event_action_filter(rule_id))
    with db_registry.cursor("runtime") as c:
        c.execute("DELETE FROM event_action_filter WHERE id=%s", (int(rule_id),))
        return int(c.rowcount or 0)


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


def upsert_repo_credential(repo_key: str, cred_type: str, value: str,
                           updated_at: str) -> dict[str, Any]:
    """仓库凭据幂等 upsert（UPDATE → rowcount==0 → INSERT，并发撞唯一约束再回退 UPDATE）。"""
    from src.kernel.id_gen import uuid7_str
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE grape_t_repo_credentials SET value=%s, updated_at=%s "
                  "WHERE repo_key=%s AND cred_type=%s", (value, updated_at, repo_key, cred_type))
        if int(c.rowcount or 0) > 0:
            return {"action": "updated", "id": 0}
        try:
            _new_id = uuid7_str()
            c.execute("INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value, updated_at) "
                      "VALUES (%s,%s,%s,%s,%s)", (_new_id, repo_key, cred_type, value, updated_at))
            return {"action": "inserted", "id": _new_id}
        except Exception:  # noqa: BLE001
            c.execute("UPDATE grape_t_repo_credentials SET value=%s, updated_at=%s "
                      "WHERE repo_key=%s AND cred_type=%s", (value, updated_at, repo_key, cred_type))
            return {"action": "updated_after_conflict", "id": 0}


def clear_repo_credential(repo_key: str, cred_type: str, updated_at: str) -> int:
    """清空仓库凭据值（value=''，保留行）。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE grape_t_repo_credentials SET value='', updated_at=%s "
                  "WHERE repo_key=%s AND cred_type=%s", (updated_at, repo_key, cred_type))
        return int(c.rowcount or 0)


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
    "autopilot_executions_88bc87": "SELECT t.repo_key, e.findings_json FROM autopilot_executions e JOIN autopilot_tasks t ON e.task_id=t.id WHERE e.id=%s",
    "autopilot_executions_018f03": "SELECT findings_json, result FROM autopilot_executions WHERE id=%s",
    "autopilot_executions_80b975": "SELECT e.id, e.task_id, t.repo_key FROM autopilot_executions e JOIN autopilot_tasks t ON e.task_id=t.id WHERE e.id=%s",
    "agent_tasks_b6b789": "SELECT event_seq, agent_type, skill, runtime_id, repo_key FROM agent_tasks WHERE id=%s LIMIT 1",
    "agent_tasks_6e9804": "SELECT issue_id, repo_key FROM agent_tasks WHERE id=%s LIMIT 1",
    "event_action_filter_3d22a6": "SELECT id, event_type, action, allow, actor, require_changes, '', handle_side_effect, priority, enabled FROM event_action_filter ORDER BY event_type, action",
    "engine_capabilities_440c87": "SELECT id, tag, priority, enabled, created_at, updated_at FROM engine_capabilities WHERE runtime_id=%s ORDER BY tag",
    "grape_t_repo_credentials_8fc7c2": "SELECT repo_key, cred_type FROM grape_t_repo_credentials WHERE id IS NULL OR id=''",
    "grape_t_repo_credentials_d590a7": "SELECT repo_key, cred_type, value FROM grape_t_repo_credentials WHERE value != ''",
    # issue 查 id（mgmt feedback 上下文）
    "issue_id_by_repo_number": "SELECT id FROM issue WHERE repo_key=%s AND issue_number=%s LIMIT 1",
    # 仓库 bot 凭据（mgmt 读；config_store 的 JOIN 拆分后也需要全量 rc 行）
    "repo_bot_token_value": (
        "SELECT value FROM grape_t_repo_credentials WHERE repo_key=%s AND cred_type='bot' LIMIT 1"),
    "repo_bot_credentials_all": (
        "SELECT repo_key, value FROM grape_t_repo_credentials WHERE cred_type='bot'"),
    # 仓库 IM 开关（mgmt 的 _apply_ak_sk 读）
    "repo_im_flags_latest": (
        "SELECT auto_impl_enabled, notify_enabled FROM repo_im_group_binding "
        "ORDER BY id DESC LIMIT 1"),
    # 账号 / 外部身份（按 engine_select._repo_user_id 的真实 SQL 登记，别自己编列名）
    "account_id_by_platform_login": (
        "SELECT ga.id FROM grape_accounts ga "
        "WHERE ga.id IS NOT NULL AND ga.id NOT IN ('', '0') AND ga.status=1 "
        "AND EXISTS (SELECT 1 FROM external_identities ei "
        "WHERE ei.grape_user_id=ga.id AND ei.provider=%s AND ei.status=1 "
        "AND LOWER(ei.external_login)=LOWER(%s) AND ei.external_login!='') LIMIT 1"),
    "grape_user_id_by_external_id": (
        "SELECT ei.grape_user_id FROM external_identities ei "
        "WHERE ei.provider=%s AND ei.status=1 AND ei.external_id=%s LIMIT 1"),
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
           "set_meta_value_any", "set_meta_if_absent", "list_featured_skills_any",
           # P4 洞察看板聚合原语（insight 只读分析域唯一取数口；任意拓扑）
           "query_events_any", "event_dashboard_metrics",
           "dashboard_task_status_counts", "dashboard_issue_status_counts",
           "dashboard_stuck_tasks", "dashboard_failed_tasks", "dashboard_task_by_source",
           "dashboard_today_status_counts", "dashboard_running_counts",
           "dashboard_avg_durations", "dashboard_workers", "dashboard_local_runtimes",
           "dashboard_runtimes_with_credential", "credential_brief", "credential_scope_owner",
           "task_event_map", "runtime_device_map", "agent_sessions_with_runtime",
           "efficiency_issue_contrib", "task_issue_attribution"]


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


def list_autopilot_executions(task_id: int | None = None, status: str = "",
                              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """autopilot 执行记录分页读（C2 表归运行域，mgmt 侧经 lib 调用）。

    列口径与 mgmt `list_executions` 一致；issue_number/agent_task_id 在旧库可能不存在，
    用 info_schema 探测后以空串占位（沿用 mgmt 侧同一兼容策略）。
    """
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    conds, args = [], []
    if task_id is not None:
        conds.append("e.task_id=%s")
        args.append(int(task_id))
    if status:
        conds.append("e.status=%s")
        args.append(status)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    with db_registry.cursor("runtime") as c:
        # MySQL 8 的 information_schema 列标签为大写（COLUMN_NAME），两种都取
        c.execute("SELECT COLUMN_NAME AS column_name FROM information_schema.columns "
                  "WHERE table_schema=DATABASE() AND table_name='autopilot_executions'")
        cols = {(r.get("column_name") or r.get("COLUMN_NAME") or "") for r in _rows(c)}
        issue_expr = "e.issue_number" if "issue_number" in cols else "'' AS issue_number"
        task_expr = "e.agent_task_id" if "agent_task_id" in cols else "'' AS agent_task_id"
        sql = ("SELECT e.id, e.task_id, e.status, e.retry_count, e.result, e.error, "
               "e.started_at, e.completed_at, e.created_at, e.agent_type, e.findings_json, "
               f"{issue_expr}, {task_expr} FROM autopilot_executions e WHERE 1=1{where} "
               "ORDER BY e.id DESC LIMIT %s OFFSET %s")
        c.execute(sql, tuple(args + [lim, off]))
        rows = c.fetchall()
        c.execute(f"SELECT COUNT(*) AS n FROM autopilot_executions e WHERE 1=1{where}", tuple(args))
        total = int(_rows(c)[0]["n"])
    keys = ("id", "task_id", "status", "retry_count", "result", "error", "started_at",
            "completed_at", "created_at", "agent_type", "findings_json", "issue_number",
            "agent_task_id")
    return {"executions": [dict(zip(keys, tuple(r))) for r in rows], "total": total,
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
    'issue_card_ensure_insert_gitcode': "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_by) SELECT COALESCE((SELECT m.user_id FROM unified_user_mapping m WHERE m.gitcode_user=? AND m.user_id IS NOT NULL AND m.user_id NOT IN ('', '0') AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=m.user_id AND ga.status=1) ORDER BY m.user_id LIMIT 1), 0), ?, ?, ?, 'active', ?",
    'issue_card_ensure_insert_github': "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, created_by) SELECT COALESCE((SELECT m.user_id FROM unified_user_mapping m WHERE m.github_login=? AND m.user_id IS NOT NULL AND m.user_id NOT IN ('', '0') AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=m.user_id AND ga.status=1) ORDER BY m.user_id LIMIT 1), 0), ?, ?, ?, 'active', ?",
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


# ── session_store 去跨域原语 ─────────────────────────────────────────────

def ensure_issue_card(repo_key: str, issue_number, assignee: str = "",
                      um_column: str = "gitcode_user") -> bool:
    """issue 卡片行建档（幂等）：无行则建最小行（status='active'）。返回是否新建。"""
    if read_one_any("issue_card_exists", (repo_key, issue_number)):
        return False
    name = ("issue_card_ensure_insert_github" if um_column == "github_login"
            else "issue_card_ensure_insert_gitcode")
    try:
        exec_any(name, (assignee, assignee, repo_key, issue_number, assignee))
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
