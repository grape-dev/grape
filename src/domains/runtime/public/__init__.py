"""runtime 域对外 lib（进程内 import，使用 runtime 自己的 DSN）。

约定（见 plan-lib-three-node §11.3，为将来切 Remote 预留）：
- 只返回 JSON 友好类型（dict/list/基础类型），不返回游标/行对象
- 列表函数支持批量或 limit/offset
- 写函数接受 idempotency_key（最终一致 + 补偿，非强事务）
"""
from __future__ import annotations
from src.kernel.config import log

import json
import time
from contextlib import contextmanager
from typing import Any

# 2026-11-19：`Request` 出现在 `require_credential(request: Request)` 的注解里。
# 本模块 `from __future__ import annotations` 使注解是字符串，但 FastAPI 在运行期
# 按**模块全局名**解析依赖签名：仅有 TYPE_CHECKING 导入会让注解停在
# ForwardRef('Request')，端点把 request 当必填 query 参数 → 全量 422，且
# `/openapi.json` 渲染抛 PydanticUserError。因此运行期必须可解析，同时 worker
# 任务路径（经 src.domains.mgmt.user.db → runtime.public）不得因缺 fastapi 崩掉：
# 顶层 try/except 导入，缺包时降级为 Any（仅 worker 场景，注解不参与业务）。
try:  # pragma: no cover - 主控有 fastapi；worker 缺包走降级分支
    from fastapi import Request
except ImportError:  # pragma: no cover - worker 无 web 框架
    Request = Any  # type: ignore[assignment,misc]

from src.kernel import db_registry
from src.kernel.issue_ident import native_issue_number


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


def cloud_sandboxes_active_count(provider_ids) -> int:
    """给定 provider id 列表里状态为 creating/running 的沙箱数。

    原实现在名为 cloud_sandboxes_active_count_by_cred 的具名查询里用子查询读 mgmt 的
    sandbox_provider_config，三库拓扑下跨库执行必失败。现在拆成：mgmt 出 provider 列表，
    本域按列表计数。
    """
    keys = [str(x) for x in (provider_ids or []) if x]
    if not keys:
        return 0
    ph = ",".join(["%s"] * len(keys))
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT COUNT(*) FROM grape_t_cloud_sandboxes WHERE provider_id IN ({ph}) "
                  f"AND state IN ('creating','running')", tuple(keys))
        return int((c.fetchone() or [0])[0] or 0)


def list_runtime_uid_rows() -> list[tuple]:
    """engine_uid 回填/校验用：id, credential_id, name, scope, owner_id, host_name, engine_uid。

    原来散在 src/api/db_setup.py 的启动期回填里直查本表；lib 模式下 SQL 只留 owner 域一份。
    """
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, credential_id, name, scope, owner_id, host_name, engine_uid "
                  "FROM agent_runtimes ORDER BY id")
        return [tuple(r) for r in c.fetchall()]


def set_runtime_engine_uid(runtime_id: int, engine_uid: str | None) -> int:
    """写单个运行时的 engine_uid（None/'' = 置空）。返回影响行数。"""
    val = engine_uid or None
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE agent_runtimes SET engine_uid=%s WHERE id=%s", (val, runtime_id))
        return int(c.rowcount or 0)


def list_runtime_name_rows() -> list[tuple]:
    """id, 展示名, name —— 引擎别名迁移/展示映射用。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, COALESCE(display_name, name), name FROM agent_runtimes ORDER BY id")
        return [tuple(r) for r in c.fetchall()]


def list_runtime_id_uid_rows() -> list[tuple]:
    """id, engine_uid —— uid 唯一性校验与批量回填用。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT id, engine_uid FROM agent_runtimes ORDER BY id")
        return [tuple(r) for r in c.fetchall()]


def runtime_ids_by_engine_uids(engine_uids) -> list[int]:
    """按 engine_uid（大小写不敏感）反查运行时 id 列表。"""
    keys = [str(u).lower() for u in (engine_uids or []) if u]
    if not keys:
        return []
    ph = ",".join(["%s"] * len(keys))
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT id FROM agent_runtimes WHERE LOWER(engine_uid) IN ({ph})", tuple(keys))
        return [int(r[0]) for r in c.fetchall()]


def runtime_cred_rows_by_engine_uids(engine_uids) -> list[tuple]:
    """按 engine_uid 精确匹配取 engine_uid, id, credential_id —— 自动编排的引擎校验用。"""
    keys = [str(u) for u in (engine_uids or []) if u]
    if not keys:
        return []
    ph = ",".join(["%s"] * len(keys))
    with db_registry.cursor("runtime") as c:
        c.execute(f"SELECT engine_uid, id, credential_id FROM agent_runtimes "
                  f"WHERE engine_uid IN ({ph})", tuple(keys))
        return [tuple(r) for r in c.fetchall()]


def rename_runtime_engine_uid(old_uid: str, new_uid: str) -> int:
    """把一个 engine_uid 的所有运行时改名为新 uid（引擎重命名级联）。返回影响行数。"""
    with db_registry.cursor("runtime") as c:
        c.execute("UPDATE agent_runtimes SET engine_uid=%s WHERE engine_uid=%s", (new_uid, old_uid))
        return int(c.rowcount or 0)


def runtime_rebase_row(runtime_id: int) -> tuple:
    """credential_id, name, scope, owner_id, host_name, engine_uid —— uid 重算需要原始行。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT credential_id, name, scope, owner_id, host_name, engine_uid "
                  "FROM agent_runtimes WHERE id=%s", (runtime_id,))
        row = c.fetchone()
        return tuple(row) if row else ()


def runtime_uid_of(runtime_id: int) -> str:
    """单个运行时的 engine_uid（缺省空串）—— 追踪打点用。"""
    with db_registry.cursor("runtime") as c:
        c.execute("SELECT COALESCE(engine_uid,'') FROM agent_runtimes WHERE id=%s", (runtime_id,))
        row = c.fetchone()
        return str(row[0] or "") if row else ""


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


# 列序必须与 mgmt._build_cred_map 的解包一一对应（14 列）：少 cred_type/platform
# 时该处抛出 "not enough values to unpack (expected 14, got 12)"，设备/引擎页整体 500。
_CRED_COLS = ("id", "name", "device_name", "scope", "owner_id", "owner_name",
              "status", "last_seen_at", "last_ip", "repo_key", "org_id", "spec",
              "cred_type", "platform")


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


def issue_brief(repo_key: str, issue_number) -> dict[str, Any] | None:
    """单 Issue 基础信息（跨域只读口，供 mgmt 组织编排使用）。"""
    num = native_issue_number(issue_number)
    with _any_cursor() as c:
        c.execute(
            "SELECT id, repo_key, issue_number, title, task_description, status, stage, "
            "scene_id, parent_issue_number FROM issue "
            "WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (repo_key, num),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0] or 0),
        "repo_key": str(row[1] or ""),
        "issue_number": native_issue_number(row[2]),
        "title": str(row[3] or ""),
        "task_description": str(row[4] or ""),
        "status": str(row[5] or ""),
        "stage": str(row[6] or ""),
        "scene_id": str(row[7] or ""),
        "parent_issue_number": native_issue_number(row[8]),
    }


def recent_issues(repo_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """某仓库最近 Issue（跨域只读口，供组织上下文渲染）。"""
    limit = max(1, min(int(limit or 100), 500))
    with _any_cursor() as c:
        c.execute(
            "SELECT issue_number, title, task_description, status, stage, created_at "
            "FROM issue WHERE repo_key=? ORDER BY issue_number DESC LIMIT ?",
            (repo_key, limit),
        )
        rows = c.fetchall()
    return [
        {
            "issue_number": native_issue_number(r[0]),
            "title": str(r[1] or ""),
            "description": str(r[2] or ""),
            "status": str(r[3] or ""),
            "stage": str(r[4] or ""),
            "created_at": str(r[5] or ""),
        }
        for r in rows
    ]


def latest_task_status(repo_key: str, issue_id: str) -> str:
    """某 Issue 最新 agent_task 状态；无任务返回空串。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT status FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (repo_key, str(issue_id)),
        )
        row = c.fetchone()
    return str(row[0] or "") if row else ""


def latest_event_brief(repo_key: str, issue_id: str) -> dict[str, Any] | None:
    """某 Issue 最新事件（seq/event_type/status/result）。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT seq, event_type, status, result FROM events "
            "WHERE repo_key=? AND issue_id=? ORDER BY seq DESC LIMIT 1",
            (repo_key, str(issue_id)),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "seq": int(row[0] or 0),
        "event_type": str(row[1] or ""),
        "status": str(row[2] or ""),
        "result": str(row[3] or ""),
    }


def event_payload(seq: int) -> dict[str, Any] | None:
    """按 seq 取事件 payload（跨域只读口）。"""
    with _any_cursor() as c:
        c.execute("SELECT payload FROM events WHERE seq=? LIMIT 1", (int(seq),))
        row = c.fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def has_inflight_repo(repo_key: str) -> bool:
    """某仓库是否仍有在途任务（解绑/迁移前守卫）。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? "
            "AND status IN ('queued','dispatched','running','pending_self_report') LIMIT 1",
            (repo_key,),
        )
        return bool(c.fetchone())


def has_inflight(repo_key: str, issue_id: str) -> bool:
    """Issue 是否还有在途任务/事件（避免把正在重试的目标误判失败）。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('pending','queued','dispatched','running') LIMIT 1",
            (repo_key, str(issue_id)),
        )
        if c.fetchone():
            return True
        c.execute(
            "SELECT 1 FROM events WHERE repo_key=? AND issue_id=? "
            "AND status IN ('queued','running') LIMIT 1",
            (repo_key, str(issue_id)),
        )
        return bool(c.fetchone())


def cancel_inflight_tasks(repo_key: str, issue_id: str) -> int:
    """取消某 Issue 在途任务（跨域写；返回影响行数）。"""
    with _any_cursor(write=True) as c:
        c.execute(
            "UPDATE agent_tasks SET status='cancelled' WHERE repo_key=? AND issue_id=? "
            "AND status IN ('queued','dispatched','running')",
            (repo_key, str(issue_id)),
        )
        return int(c.rowcount or 0)


def inflight_tasks(repo_key: str, issue_id: str) -> list[tuple]:
    """某 Issue 在途任务 (id, event_seq)，供跨域取消/重试使用。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT id, event_seq FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('queued','dispatched','running')",
            (repo_key, str(issue_id)),
        )
        return [tuple(r) for r in c.fetchall()]


def clear_event_active_guard(repo_key: str, issue_id: str) -> int:
    """清某 Issue 的活跃事件守卫（跨域写；返回影响行数）。"""
    with _any_cursor(write=True) as c:
        c.execute(
            "DELETE FROM event_active_guard WHERE repo_key=? AND issue_id=?",
            (repo_key, str(issue_id)),
        )
        return int(c.rowcount or 0)

# 相关技能候选池大小：热度前 N 进打分池（与 mgmt 路由 _RELATED_POOL_SIZE 同值；
# 全库技能 ~10² 量级，300 覆盖全部活跃技能。QA B2：裸数字须具名+来源）。
_SKILL_RELATED_POOL_SIZE = 300

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

# 账号删除级联的四张表（表名/语句只在本模块，调用方不可传入表名或拼接 SQL）
_USER_PURGE_PLAN: tuple[tuple[str, str], ...] = (
    ("agent_tasks", "user_purge_agent_tasks"),
    ("chat_agent_session", "user_purge_chat_agent_session"),
    ("chat_conversations", "user_purge_chat_conversations"),
    ("issue", "user_purge_issue"),
)


def purge_user_rows(user_id: str, cursor: Any = None) -> dict[str, int]:
    """账号删除级联：按 user_id 一并清理 runtime 域业务数据。

    覆盖 agent_tasks / chat_agent_session / chat_conversations / issue 四表。原实现是
    mgmt 侧在同一事务里逐表 DELETE —— 拆成四条意图会丢"一起删"的语义，故做成一个原语。

    任意拓扑：
      - 自有 DSN → `_any_tx()` 真事务（四表同生共死，不再逐条 autocommit）；
      - 单库/测试拓扑且调用方传入游标 → **复用调用方游标与事务**（账号匿名化在
        mgmt 事务里"一起提交/回滚"，不提前 commit 架空调用方 rollback）；
      - 单库/测试拓扑未传游标 → 进程库 + `db_lock`，由本原语提交。
    """
    uid = str(user_id)
    out: dict[str, int] = {}

    def _run(c: Any) -> None:
        for table, name in _USER_PURGE_PLAN:
            c.execute(_any_sql(name), (uid,))
            out[table] = int(getattr(c, "rowcount", 0) or 0)

    if db_registry.uses_own_dsn("runtime"):
        with _any_tx() as c:
            _run(c)
        return out
    if cursor is not None:
        _run(cursor)
        return out
    with _any_cursor(write=True) as c:
        _run(c)
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


def update_skill_category_tags(skill_id: int, cat_l1_id: int = 0, cat_l2_id: int = 0,
                               tags: str = "") -> int:
    """写入技能的一/二级分类与标签（Issue #2761；tags 为 JSON 字符串）。

    走任意拓扑具名写语句：三库（自有 DSN）与单库/测试拓扑都可用。
    """
    return exec_any("skill_category_tags_update",
                    (int(cat_l1_id or 0), int(cat_l2_id or 0), tags or "",
                     int(skill_id)))


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
                              owner_name: str, repo_key: str, org_id: str,
                              cred_type: str = "worker", platform: str = "",
                              credential_uid: str = "") -> dict[str, Any]:
    """新建 runtime 凭据。名称全局唯一：撞唯一约束时返回 duplicate（**不抛异常**），
    调用方据此回 400 —— 若这里抛异常，意图会被当成可重试失败而无限重放。

    org_id 归一（D105 第三批后 TEXT 列）：org_id 列已 TEXT 化（''=未归属哨兵，
    db_setup 幂等补列 + 迁移 2026111700xx 同形）。「个人设备/未归属组织」空串直存
    ''；uuid/组织 id 字符串原样存。**不再 int() 强转**——旧 int 列时代空串写 int 会
    1366 才做的 0 归一，在 TEXT 列上会产出 '0' 脏值：可见性过滤按新口径
    （org_id in ('', <org uuid>)）比对时 '0' 永不匹配 → 个人组织下 global 凭据
    全部不可见（s3/s4 生成后页面消失的实测根因）。历史 '0' 行由调用方可见性
    过滤按哨兵兼容（见 settings 列表端点），本函数不再制造新脏值。
    """
    _org_val = str(org_id).strip() if str(org_id if org_id is not None else "").strip() not in ("", "None") else ""
    with _any_cursor(write=True) as c:
        try:
            _rc_cols = ["token_hash", "name", "scope", "owner_id", "owner_name", "repo_key", "org_id"]
            _rc_vals = [token_hash, name, scope, owner_id, owner_name, repo_key, _org_val]
            # [沙箱迁移 i7] cred_type/platform 列；#646 credential_uid 稳定键——旧库缺列由迁移兜底
            for _col, _val in (("status", "active"),
                               ("cred_type", cred_type or "worker"), ("platform", platform or ""),
                               ("credential_uid", credential_uid or "")):
                _rc_cols.append(_col)
                _rc_vals.append(_val)
            c.execute(f"INSERT INTO runtime_credentials ({', '.join(_rc_cols)}) "
                      f"VALUES ({','.join('?' * len(_rc_cols))})",
                      tuple(_rc_vals))
            return {"action": "inserted", "id": int(c.lastrowid or 0)}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "UNIQUE" in msg or "1062" in msg or "Duplicate" in msg:
                return {"action": "duplicate", "id": 0}
            raise


def credential_usage_log_insert_many(rows: list) -> None:
    """批量写入凭据使用审计行（参数化，空列表直接返回）。

    归属：表属 runtime 域，写语句由本域持有；kernel 的异步 flush 只负责攒批与调用本函数。
    连接语义：`_any_cursor(write=True)` 走 db_registry 的**线程本地**连接（三库自有 DSN 为
    autocommit，单库/测试回落进程库），因此后台线程与主线程事务互不干扰 —— 与原先 kernel
    侧「后台线程独立连接」的诉求一致，不再需要按 backend 手写 sqlite3 分支。

    rows 列序固定为：(id, credential_id, credential_kind, user_id, repo_key,
                       initiating_actor_id, executor_context, operation, created_at)
    """
    if not rows:
        return
    with _any_cursor(write=True) as c:
        c.executemany(
            "INSERT INTO grape_t_credential_usage_log (id, credential_id, credential_kind, "
            "user_id, repo_key, initiating_actor_id, executor_context, operation, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)", rows)


def credential_usage_log_prune(cutoff: str, batch: int = 500) -> int:
    """删除 created_at < cutoff 的凭据使用审计行，返回删除行数。

    两步查删（SELECT id LIMIT → DELETE IN 具体列表）：MySQL 禁止 IN 子查询带 LIMIT（1235），
    SQLite 也不支持同形态，两步法双方言安全。

    归属：grape_t_credential_usage_log 属 runtime 域，SQL 必须由本域持有。
    调用方（kernel 的保留期清理）此前直接对着该表发 SQL，是最后一处真实跨域基表直查；
    这里提供具名原语后，调用方只依赖本函数签名。
    """
    total = 0
    with _any_cursor(write=True) as c:
        while True:
            c.execute(
                "SELECT id FROM grape_t_credential_usage_log WHERE created_at < ? LIMIT ?",
                (cutoff, batch))
            ids = [r[0] for r in (c.fetchall() or [])]
            if not ids:
                break
            placeholders = ",".join("?" for _ in ids)
            c.execute(
                f"DELETE FROM grape_t_credential_usage_log WHERE id IN ({placeholders})",
                ids)
            n = int(getattr(c, "rowcount", 0) or 0)
            total += n
            if n < batch:
                break
    return total


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


# ---------- 本机指标上报的任意拓扑原语（insight 进程 15s 心跳） ----------
# 上面两个 `ensure_device_credential` / `touch_credential_metrics` 直连
# `db_registry.cursor("runtime")`，只在 runtime 自有 DSN 下可用（单库拓扑抛
# RuntimeError）。insight 进程的 server_metrics 循环原来在**进程库**里直查/直写
# runtime_credentials：三库拓扑下进程库没有该表（视图 0）→ 1146 → 被外层 except
# 吞成「register failed / upload failed」，本机 device 常量从未登记、指标从未刷新。
# 下面三个走 `_any` 族（自有 DSN 优先、单库回落进程库），SQL 只留本模块一份。


def device_credential_id_any(hostname: str) -> int | None:
    """本机 device 凭据行 id（scope='global'，任意拓扑）。

    与原语句逐字一致：`WHERE device_name=? AND scope='global'`，只取第一行
    （原实现 `fetchone()`，不加 LIMIT —— 保持可逐字比对）。
    """
    row = read_one_any("runtime_credential_device_id", (hostname,))
    return int(row[0]) if row else None


def register_device_credential_any(hostname: str, token_hash: str, last_ip: str) -> dict[str, Any]:
    """幂等登记本机 device 凭据行（scope='global'，任意拓扑）。

    口径与原实现逐点一致：先按 (device_name, scope='global') 判存，命中即**不写**
    （返回 exists）；缺失才 INSERT（name=device_name、status='active'、last_ip）。
    `name` 带 UNIQUE 约束，同机换 scope 的存量行会让 INSERT 撞键——原实现由外层
    except 吞成「register failed」，这里收敛成 duplicate 值返回；其余异常照旧上抛
    （不静默降级）。SELECT+INSERT 同一 `_any_tx()`：自有 DSN 真事务。
    """
    with _any_tx() as c:
        c.execute(_any_sql("runtime_credential_device_id"), (hostname,))
        row = c.fetchone()
        if row:
            return {"action": "exists", "id": int(row[0])}
        try:
            c.execute(_any_sql("runtime_credential_register_device"),
                      (token_hash, hostname, hostname, last_ip))
        except Exception as exc:  # noqa: BLE001 - 撞键是正常值，其它必须上抛
            _s = str(exc).upper()
            if "UNIQUE" in _s or "1062" in _s or "DUPLICATE" in _s:
                log.debug(f"register_device_credential_any duplicate: {hostname}")
                return {"action": "duplicate", "id": 0}
            raise
        return {"action": "inserted", "id": int(getattr(c, "lastrowid", 0) or 0)}


def touch_device_credential_any(hostname: str, last_seen_at: str, last_ip: str = "",
                                cpu_percent: float = 0, memory_percent: float = 0,
                                disk_percent: float = 0, tmp_percent: float = 0,
                                bandwidth_percent: float = 0, bandwidth_mbps: float = 0,
                                load_avg: str = "") -> int:
    """刷新 device 凭据在线指标（scope='global'，任意拓扑；同值重放无副作用）。

    参数顺序 = 原 UPDATE 的占位符顺序（列序逐字一致，调用方按位置传）。
    返回受影响行数：**从游标读 `rowcount`**，不取 `exec_any` 的返回值——pymysql 的
    `execute()` 返回 int（不是游标），`exec_any` 的 `getattr(cur,'rowcount',0)` 在自有
    DSN 下恒为 0（该缺陷属 `exec_any` 本体，三域同名原语均存在，本轮不扩大语义面）。
    """
    with _any_cursor(write=True) as c:
        c.execute(_any_sql("runtime_credential_touch_device"),
                  (last_seen_at, last_ip, cpu_percent, memory_percent, disk_percent,
                   tmp_percent, bandwidth_percent, bandwidth_mbps, load_avg, hostname))
        return int(getattr(c, "rowcount", 0) or 0)


# ---------- 引擎行写原语（P3-5f-2：mgmt 进程写 runtime 基表 agent_runtimes） ----------

def _sync_local_derived_tags(c, rid: int, name: str, scope: str,
                             engine_uid: str = "") -> None:
    """Issue #1208：本机直连引擎（credential_id=0）补派生 engine:/scope:/scene: 标签。

    本机直连引擎既不走 worker 注册链（runtime.py 注册分支），也没有 worker 心跳——
    心跳链的派生刷新按 `r.credential_id=?` 过滤，credential_id=0 的行永远匹配不到，
    于是这些行只带平铺 'all' 行、没有任何冒号派生行。selector 按 engine:<name> 圈池时
    它们恒判「标签不匹配」（reason=selector），命中预览里本机引擎永远进不了命中组。

    本机探测快照 upsert 是这些行唯一的写入口，所以在 UPDATE/INSERT 两处都补这一步；
    sync_derived_agent_tags 自带写放大防护与幂等替换，失败只告警，不阻断探测快照落库。
    """
    try:
        import json as _json

        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        c.execute("SELECT scene_tags, runtime_info FROM agent_runtimes WHERE id=?", (rid,))
        row = c.fetchone()
        scene_tags = row[0] if row else ""
        engine_type = ""
        try:
            engine_type = (_json.loads((row[1] if row and len(row) > 1 else "") or "{}")
                           .get("engine_type") or "")
        except Exception:  # noqa: BLE001 - runtime_info 脏数据按无 engine_type 处理
            engine_type = ""
        sync_derived_agent_tags(c, rid, name, scope, scene_tags,
                                engine_uid=engine_uid or "", engine_type=engine_type)
    except Exception as e:  # noqa: BLE001 - 派生失败不阻断本机探测快照
        log.warning(f"upsert_local_runtimes: agent_tags derive failed for runtime {rid}: {e}")


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
        # Issue #646：engine_uid 派生轨道（UUIDv5 按自然键确定性派生，幂等防 id 漂移）；
        # 旧库缺列时整段 uid 逻辑降级跳过（与 legacy 双读路径一致）。
        from src.kernel.engine_uid import derive_engine_uid
        _uid_ready = False
        try:
            c.execute("SELECT engine_uid FROM agent_runtimes LIMIT 1")
            _uid_ready = True
        except Exception:
            _uid_ready = False
        for a in agents or []:
            name = (a.get("name") or "").strip().lower()
            if not name:
                continue
            names.append(name)
            _engine_uid = derive_engine_uid("", name, scope, owner_id, host_name)
            _ls = _json.dumps(a.get("local_skills") or [], ensure_ascii=False)
            _detail = a.get("local_skills_detail")
            _detail_json = _json.dumps(_detail, ensure_ascii=False) if _detail is not None else ""
            row = None
            if _uid_ready:
                # uid 优先匹配（稳定键，幂等防漂移）；未迁移库回退 legacy 路径
                c.execute("SELECT id FROM agent_runtimes WHERE engine_uid=? LIMIT 1",
                          (_engine_uid,))
                row = c.fetchone()
            if not row:
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
                    # probe_* 必须一起写回：本地探测是直连引擎唯一的健康来源，
                    # 缺了它 probe_status 永远 pending → claim 侧把"未探测"当低分 → 拒绝认领
                    # （2026-11-15 实证：本机 4 个直连引擎全部 pending，任务无人认领或认领后不执行）。
                    "UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, "
                    "healthy=?, pip_package=?, updated_at=?, local_skills=?, "
                    "skill_scan_result=?, host_name=?, "
                    "probe_status=?, probe_latency=?, probe_error=?, probe_last=?, "
                    "probe_ok_streak=CASE WHEN ? THEN COALESCE(probe_ok_streak,0)+1 ELSE 0 END "
                    "WHERE id=?",
                    (a.get("display_name", name), 1 if a.get("installed") else 0,
                     a.get("cli_path", ""), a.get("version", ""),
                     1 if a.get("healthy") else 0, a.get("pip_package", ""), now_iso,
                     _ls, _detail_json, host_name,
                     (a.get("probe_status") or ("ok" if a.get("healthy") else "failed")),
                     int(a.get("probe_latency") or 0),
                     a.get("probe_error", ""), now_iso,
                     1 if a.get("healthy") else 0,
                     rid))
                if _uid_ready:
                    # Issue #646：uid 派生轨道写回（UPDATE 路径）
                    c.execute("UPDATE agent_runtimes SET engine_uid=? WHERE id=?",
                              (_engine_uid, rid))
                # Issue #1208：本机直连引擎补三维度派生标签（否则 selector 圈池恒不命中）
                _sync_local_derived_tags(c, rid, name, scope, _engine_uid)
                out["upserted"] += 1
                out["ids"][name] = rid
                continue
            if not a.get("installed"):
                continue
            if _uid_ready:
                c.execute(
                    "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, "
                    "pip_package, scope, owner_id, local_skills, skill_scan_result, host_name, "
                    "last_seen_at, updated_at, probe_status, probe_latency, probe_last, probe_ok_streak, engine_uid) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, a.get("display_name", name), 1, a.get("cli_path", ""), a.get("version", ""),
                     1 if a.get("healthy") else 0, a.get("pip_package", ""), scope, owner_id,
                     _ls, _detail_json, host_name, now_iso, now_iso,
                     (a.get("probe_status") or ("ok" if a.get("healthy") else "failed")),
                     int(a.get("probe_latency") or 0), now_iso,
                     1 if a.get("healthy") else 0, _engine_uid))
            else:
                c.execute(
                "INSERT INTO agent_runtimes (name, display_name, installed, cli_path, version, healthy, "
                "pip_package, scope, owner_id, local_skills, skill_scan_result, host_name, "
                "last_seen_at, updated_at, probe_status, probe_latency, probe_last, probe_ok_streak) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, a.get("display_name", name), 1, a.get("cli_path", ""), a.get("version", ""),
                 1 if a.get("healthy") else 0, a.get("pip_package", ""), scope, owner_id,
                 _ls, _detail_json, host_name, now_iso, now_iso,
                 # 新引擎首写就带上探测结果，避免"插入即 pending"再走一轮认领被拒
                 (a.get("probe_status") or ("ok" if a.get("healthy") else "failed")),
                 int(a.get("probe_latency") or 0), now_iso,
                 1 if a.get("healthy") else 0))
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
            # Issue #1208：新建的本机直连引擎同样补三维度派生标签（与 UPDATE 路径同口径）
            _sync_local_derived_tags(c, rid, name, scope, _engine_uid)
            out["inserted"] += 1
            out["ids"][name] = rid

        if names:
            ph = ",".join(["?"] * len(names))
            # Issue #646：过期行删除走显式清理链（池/能力/会话/绑定/场景 + 对账报告），
            # MySQL RESTRICT FK 兜底防漏（不静默级联蒸发）——与供体 save_agent_runtimes 口径一致。
            # 必须在 DELETE 之前执行（行删掉后 stale 集合查不到了）。
            try:
                _stale_ids = [r[0] for r in c.execute(
                    f"SELECT id FROM agent_runtimes WHERE scope=? AND owner_id=? AND credential_id=0 "
                    f"AND host_name=? AND lower(name) NOT IN ({ph})",
                    (scope, owner_id, host_name, *names)).fetchall()]
                if _stale_ids:
                    # 同域：runtime 引用对账归本模块 public 原语（不再绕 mgmt.user.db）
                    reconcile_for_caller(c, _stale_ids, 0, reason="refresh stale engines")
            except Exception as _se:
                log.warning(f"upsert_local_runtimes stale-cleanup rebind skipped: {_se}")
            c.execute(f"DELETE FROM agent_runtimes WHERE scope=? AND owner_id=? AND credential_id=0 "
                      f"AND host_name=? AND lower(name) NOT IN ({ph})",
                      (scope, owner_id, host_name, *names))
            out["pruned"] = int(c.rowcount or 0)
        # Issue #646：DELETE 前先把待删行的跨表引用改绑到保留行（孤儿引用/静默失效防护）
        try:
            _dup_rows = c.execute(
                "SELECT id, lower(name), scope, owner_id, COALESCE(org_id, 0), COALESCE(host_name, '') "
                "FROM agent_runtimes WHERE credential_id=0 AND id NOT IN "
                "(SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 "
                "GROUP BY lower(name), scope, owner_id, COALESCE(org_id, 0), COALESCE(host_name, ''))"
            ).fetchall()
            for _dup_id, _n, _sc, _ow, _org, _host in _dup_rows:
                _keep = c.execute(
                    "SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 AND lower(name)=? "
                    "AND scope=? AND owner_id=? AND COALESCE(org_id, 0)=? AND COALESCE(host_name, '')=?",
                    (_n, _sc, _ow, _org, _host)).fetchone()
                if _keep and _keep[0] and _keep[0] != _dup_id:
                    # 同域：runtime 引用对账归本模块 public 原语（不再绕 mgmt.user.db）
                    reconcile_for_caller(c, [_dup_id], _keep[0])
        except Exception as _re:
            log.warning(f"upsert_local_runtimes rebind-before-dedup skipped: {_re}")
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
    "runtime_credentials_5b448f": "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, last_ip, repo_key, org_id, spec, cred_type, platform FROM runtime_credentials",
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
                    "like_count", "updated_at", "org_id", "summary")


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


def runtime_ids_by_engine_uids(uids: list[str]) -> list[int]:
    """engine_uid 列表 → agent_runtimes.id 列表（缺失项以 0 哨兵占位）。

    scene_precheck 的 uid-first 解析经此公开读原语取数，不再跨域调用本模块的
    私有游标；lib 模式下亦可经注册表解析。
    """
    _uids = [u for u in (uids or []) if str(u or "").strip()]
    if not _uids:
        return []
    placeholders = ",".join("?" * len(_uids))
    with _any_cursor() as c:
        c.execute(
            f"SELECT id, engine_uid FROM agent_runtimes WHERE engine_uid IN ({placeholders})",
            tuple(_uids),
        )
        rows = c.fetchall()
    found = {row[1]: row[0] for row in rows}
    return [int(found[u]) if found.get(u) else 0 for u in _uids]


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
           "runtime_credentials_visible_rows",
           "list_engine_tags", "query_events", "count_events", "group_events", "get_autopilot_execution", "upsert_engine_capability",
           "delete_engine_capability", "update_engine_capability", "renew_channel_lease", "acquire_channel_lease", "release_channel_lease",
           "release_all_leases", "heartbeat_instance", "list_active_instances", "health",
           "runtime_ids_by_engine_uids",
           # P3-5g 写原语（mgmt / insight 进程写 runtime 基表）
           "purge_user_rows", "delete_chat_conversations", "delete_vod_conversations",
           "detach_agent_sessions", "clear_agent_session_runtime", "ensure_agent_session",
           "stamp_task_traces", "reconcile_engine_references",
           # 飞书扫码 onboarding 换绑清理（任意拓扑原语；SQL 只留本模块）
           "vod_session_id_by_repo_any", "chat_conversations_delete_by_im_channel_any",
           "vod_conversation_delete_by_session_any", "vod_agent_session_ensure_any",
           # P3-5h 写原语
           "mark_detach_rebound", "delete_autopilot_executions", "set_autopilot_findings",
           "set_repo_im_notifications", "add_event_action_filter",
           "update_event_action_filter", "delete_event_action_filter",
           # P3-5f 写原语（skill / 凭据）
           "update_skill_content", "update_skill_obs", "update_skill_category_tags", "insert_skill", "create_skill_row",
           "delete_skill_row", "delete_skill_by_id", "adjust_skill_like_count",
           "insert_runtime_credential", "rename_runtime_credential",
           "set_runtime_credential_scope", "delete_runtime_credential",
           "upsert_repo_credential", "clear_repo_credential",
           "ensure_device_credential", "touch_credential_metrics",
           # 本机指标上报的任意拓扑版（api/server_tasks.py insight 进程 15s 心跳）
           "device_credential_id_any", "register_device_credential_any",
           "touch_device_credential_any",
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
           "efficiency_issue_rows", "efficiency_issue_detail_rows", "efficiency_task_rows",
           # IM 巡检执行卡（runtime/im/handler_async.py；任意拓扑）
           "autopilot_execution_detail_any",
           # 指标桶取数（insight/observability/metric_rollup；任意拓扑）
           "task_status_counts_by_created_any", "task_status_counts_by_completed_any",
           "task_timeout_counts_by_repo_any", "task_timing_rows_any",
           "event_cascade_counts_by_repo_any", "online_credential_count_any",
           # api server_common 运维诊断（/healthz 表探活 + /health/realtime 全量统计）
           "health_task_rows_total", "health_task_status_counts",
           "health_task_inflight_count", "health_runtime_rows",
           "task_sampler_snapshot_any",
           # issue 按 id 批量取列（api/feedback_status.py；跨域 JOIN 拆分，任意拓扑）
           "issue_stage_status_rows",
           # 跨域 JOIN 拆分右表读口（insight token 归因：agent_tasks / runtime_credentials）
           "task_issue_id_rows", "runtime_credential_display_names",
           # 流水线业务状态 / 阶段任务引用（api/pipeline_status.py；issue/issue_state/agent_tasks 本域）
           "issue_business_stage_status_any", "issue_state_stage_any", "segment_task_rows_any",
           # A2A 派发门禁读路径（api/a2a_dispatch_guard.py；events / event_skipped 本域）
           "a2a_active_event_rows", "a2a_active_dup_rows", "a2a_window_dedup_rows",
           "a2a_storm_event_times", "a2a_storm_skip_rows",
           # insight 数据缺失探测基集（observability/gap.py；本域两张表）
           "runtime_credentials_gap_rows_any", "runtime_credentials_silent_rows_any",
           "agent_task_activity_rows_any", "runtime_credentials_gap_rows",
           "runtime_credentials_silent_rows", "agent_task_activity_rows",
           # 免鉴权健康端点就绪快照（insight/routes/health.py；任意拓扑）
           "agent_runtime_health_snapshot_any",
           # 设备资源曲线采样基集（insight/observability/rollup.py；任意拓扑）
           "runtime_credential_sample_rows_any",
           # 总览聚合基集（insight/observability/overview.py；agent_tasks / runtime_credentials 本域）
           "obs_overview_task_rows_any", "obs_overview_credential_status_rows_any",
           # 场景节点技能 scope 判定（src/api/scene_skill_scope.py）
           "skill_agent_type_by_priority_any", "skill_rebind_candidate_rows_any",
           # QA 结论门禁读口（src/api/qa_gate.py；events 本域）
           "qa_gate_latest_event_any", "qa_gate_latest_pr_payload_any",
           # 接管会话状态（src/api/git_repo_api.py；takeover_sessions 本域）
           "takeover_session_status_any",
           # 接管会话心跳（src/domains/mgmt/routes/takeover.py；takeover_sessions 本域）
           "takeover_touch_updated_at_any",
           # 账号注销前置：在制任务数（src/domains/mgmt/routes/grape_auth.py；agent_tasks 本域）
           "inflight_task_count_any",
           # 派发前 runtime_id 有效性（src/api/pipeline_executor.py；agent_runtimes 本域）
           "runtime_heartbeat_alive_any",
           # 组织下派/上下文跨域写口（A10/A11 整改）
           "cancel_issue_core", "cancel_task", "cancel_direct_task", "run_pipeline",
           "run_git", "clone_auth_scheme_for", "storage_save_bytes", "storage_delete_object"]


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
        # Issue #3097：mgmt 删用户/合并账号时引用对账失败的重放（幂等）
        "engine_refs_reconcile": lambda p: reconcile_engine_references(
            list(p.get("runtime_ids") or []), int(p.get("keep_id") or 0),
            reason=str(p.get("reason") or "")),
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
            "is_featured", "like_count", "category_id",
            "cat_l1_id", "cat_l2_id", "tags", "summary")
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
            "category_id": r["category_id"] or 0,
            "cat_l1_id": r.get("cat_l1_id") or 0,
            "cat_l2_id": r.get("cat_l2_id") or 0,
            "tags": r.get("tags") or "",
            "summary": r.get("summary") or ""}


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
    # ── 免鉴权健康端点的运行时就绪快照（insight/routes/health.py）──
    # `_READ_QUERIES` 里同名键是 %s 方言的 lib 专用版（单库拓扑经 db_registry.cursor
    # 会 RuntimeError）；这里登记的是**可移植**同名语句（零参数、任意拓扑）。
    "agent_runtimes_34b97d": (
        "SELECT name, status, healthy FROM agent_runtimes "
        "WHERE status='online' AND healthy=1 "
        "ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END "
        "LIMIT 1"),
    # 同上：取不到旗舰行时区分 missing（库内无任何 runtime 行）与 unhealthy。
    "agent_runtimes_total_count": "SELECT COUNT(*) FROM agent_runtimes",
    # 派发前 runtime_id 有效性校验（src/api/pipeline_executor.py）。
    # 列序/谓词照抄原直发句：只取 last_seen_at 一列，status 过滤留在 SQL（走既有索引）。
    # 心跳新鲜度的解释权在薄函数 runtime_heartbeat_alive_any（本域 parse_dt + 阈值）。
    "runtime_last_seen_online": (
        "SELECT last_seen_at FROM agent_runtimes WHERE id=? AND status='online'"),
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
        "SELECT id, runtime_id, skill, skill_id, COALESCE(engine_uid,'') FROM chat_agent_session "
        "WHERE id=? AND user_id=? AND (repo_key='' OR repo_key IS NULL)"),
    'chat_config_row_canonical_legacy': (
        "SELECT id, runtime_id, skill, skill_id FROM chat_agent_session "
        "WHERE id=? AND user_id=? AND (repo_key='' OR repo_key IS NULL)"),
    'chat_config_rows_by_user': (
        "SELECT id, runtime_id, skill, skill_id, COALESCE(engine_uid,'') FROM chat_agent_session "
        "WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) "
        "ORDER BY last_active_at DESC, id DESC"),
    'chat_config_rows_by_user_legacy': (
        "SELECT id, runtime_id, skill, skill_id FROM chat_agent_session "
        "WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) "
        "ORDER BY last_active_at DESC, id DESC"),
    'agent_runtime_name_by_uid': "SELECT name FROM agent_runtimes WHERE engine_uid=?",
    'chat_config_upsert_uid': (
        "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid, skill, "
        "skill_id, created_at, last_active_at) "
        "VALUES (?,?,?,?,?,?,?,datetime('now'),datetime('now'))"),
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
    'conversation_delete_by_im': 'DELETE FROM chat_conversations WHERE im_channel_id=?',
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
    # 任务维度取该轮全部消息（chat 任务状态/SSE 兜底：跨机器或进程重启后内存无常驻任务）
    # chat 配置面（引擎/技能下拉）：三张表都归 runtime，mgmt 进程直查会 1146 → 下拉永远空
    'runtime_credential_type_by_id': "SELECT cred_type FROM runtime_credentials WHERE id=?",
    # G-02（A11 裁定 2026-11-17：grape_t_cloud_sandboxes 归 runtime）：mgmt 吊销链 409 门禁
    # 的低频读点改走本 public 面。mgmt 侧禁止再直连该表（foreign_table_refs 收缩方向）。
    # [跨域拆解 2026-11-18] 原来的具名查询用子查询读 mgmt 的 sandbox_provider_config ——
    # 跨库必然失败（服务的是读，不是 DDL）。改为具名函数 cloud_sandboxes_active_count()，
    # provider 列表由 mgmt.public 提供，调用方编排。
    # G-02（A11）：runtime_credentials 归 runtime，mgmt 侧沙箱 Provider 端点族对它的
    # JOIN/子查询全部收敛为以下具名读（mgmt 库视图依赖随之消除，单库/三库均可移植）。
    # brief 全量 map：管理列表页单请求取一次，行数 = 凭据数（管理面小表）。
    'sandbox_provider_cred_brief_map': (
        "SELECT id, name, scope, owner_name, owner_id, repo_key FROM runtime_credentials"),
    'runtime_credential_owner_name_by_id': "SELECT owner_name FROM runtime_credentials WHERE id=?",
    'runtime_credential_scope_by_token': (
        "SELECT id, scope FROM runtime_credentials WHERE token_hash=? AND status='active'"),
    'runtime_credential_all_ids': "SELECT id FROM runtime_credentials",
    'runtime_credential_uid_by_id': "SELECT credential_uid FROM runtime_credentials WHERE id=?",
    'runtime_credential_id_uid_pairs': "SELECT id, credential_uid FROM runtime_credentials",

    'agent_runtime_online_by_cred': "SELECT 1 FROM agent_runtimes WHERE credential_id=? AND status='online' LIMIT 1",
    'chat_local_engines': (
        "SELECT id, name, version, COALESCE(host_name,'') FROM agent_runtimes "
        "WHERE credential_id=0 AND installed=1 AND healthy=1 ORDER BY id"),
    'chat_user_engines': (
        "SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
        "WHERE rc.scope = 'user' AND rc.status='active' AND rc.owner_name = ?"),
    # ↑ Issue #868 降级契约（2026-11-16 E2E 回归实锤）：引擎列表 SQL 不硬引用
    # engine_uid——旧库缺列时整段查询塌空（backends=[]）。uid 统一由 chat.py 的
    # _ar_engine_uid(rid) 补（自带缺列/缺行降级 ''），单源同尺。
    'chat_engine_by_id': (
        "SELECT ar.name, ar.version, rc.device_name, rc.owner_name, ar.credential_id, rc.scope "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
        "WHERE ar.id=?"),
    # Issue #868 指针外引擎反查 uid（chat 守卫口径：chat.py 不得直查 agent_runtimes，
    # 走本登记层；缺列/缺行由调用侧 except 降级 ''，旧库语义不变）
    'chat_engine_uid_by_id': (
        "SELECT COALESCE(engine_uid,'') FROM agent_runtimes WHERE id=?"),
    # 旧库降级路径（engine_uid 列未迁移时 get_chat_config 的 fallback 查询；
    # 与 chat_local_engines/chat_user_engines 同构、不含 engine_uid 列）
    'chat_local_engines_legacy': (
        "SELECT id, name, version, COALESCE(host_name,'') FROM agent_runtimes "
        "WHERE credential_id=0 AND installed=1 AND healthy=1 ORDER BY id"),
    'chat_user_engines_legacy': (
        "SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
        "WHERE rc.scope='user' AND rc.status='active' AND rc.owner_name = ?"),
    'chat_skill_id_scope': (
        "SELECT id FROM skill WHERE skill_name=? AND scope=? AND is_active=1 LIMIT 1"),
    'chat_skill_id_scope_owner': (
        "SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? "
        "AND is_active=1 LIMIT 1"),
    'chat_skills_all': (
        "SELECT skill_name, display_name, description, skill_path, scope, COALESCE(owner_id,'') "
        "FROM skill WHERE is_active=1 AND agent_type='chat' ORDER BY scope, skill_name"),
    'chat_skill_scope_by_id': 'SELECT scope FROM skill WHERE id=? AND is_active=1',
    'messages_by_task': (
        # 列序固定（tests/arch/test_chat_domain_reads.py 守）：0=id 1=role
        # 2=generate_status 3=content 4=conversation_id；5=created_at 为本轮新增的
        # 末列（孤儿轮次时间兜底用，老消费者按下标取前 5 列不受影响）。
        "SELECT id, role, generate_status, content, conversation_id, created_at "
        "FROM chat_messages WHERE task_id=? ORDER BY id"),
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
    'repo_im_delete_by_app': 'DELETE FROM repo_im_group_binding WHERE im_app_id=?',
    'bot_seen_delete_by_app': 'DELETE FROM bot_seen_messages WHERE app_id=?',
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
    'devops_agent_session_row': 'SELECT id, runtime_session_id, status, last_active_at, enable, engine_uid FROM devops_agent_session WHERE repo_key=? AND agent_type=?',
    'devops_agent_session_row_legacy': 'SELECT id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?',
    'devops_agent_sessions_by_repo': 'SELECT id, agent_type, runtime_id, runtime_session_id, status, last_active_at, enable FROM devops_agent_session WHERE repo_key=? ORDER BY agent_type',
    # 幂等：ensure_agent_session 语义是「已存在则不动」，SQL 必须带 OR IGNORE
    #（与本文件 devops_agent_session_insert_vod 及历史 INSERT IGNORE 对齐）。
    # 曾退化成普通 INSERT：重复建同一会话直接撞主键抛异常，而调用方按 0/1 语义处理，
    # 于是「幂等建会话」在最需要幂等的抢占/重放路径上表现为失败。
    'devops_agent_session_insert': "INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token, status, enable, subscribe_events, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
    'devops_agent_session_delete_by_repo_agent': 'DELETE FROM devops_agent_session WHERE repo_key=? AND agent_type=?',
    'devops_agent_session_delete_by_repo': 'DELETE FROM devops_agent_session WHERE repo_key=?',
    # ── 飞书扫码 onboarding 换绑清理（mgmt/routes/feishu_onboard.py）───────────
    # 三张表（chat_conversations / vod_conversations / devops_agent_session）全归本域。
    # 原调用点是「lib 可用走 lib（%s 具名查询）+ 否则本地兜底 SQL 打进程主库」，
    # 兜底 SQL 才是跨域统计来源；三库拓扑下兜底侧必 1146（视图 0）。
    # 语句与改造前兜底 SQL 逐字一致（列序/谓词/无 ORDER·LIMIT），只换 `?` 占位符；
    # INSERT 用 OR IGNORE 与 lib 侧 `ensure_agent_session` 的 INSERT IGNORE 对齐。
    "conversation_delete_by_im_channel": (
        "DELETE FROM chat_conversations WHERE im_channel_id=?"),
    "vod_conversation_delete_by_session": (
        "DELETE FROM vod_conversations WHERE agent_session_id=?"),
    "devops_agent_session_insert_vod": (
        "INSERT OR IGNORE INTO devops_agent_session "
        "(id, repo_key, agent_type, status, last_active_at) "
        "VALUES (?,?,'vod',?,datetime('now'))"),
    # ── 账号删除级联（mgmt/admin/cooldown.py 匿名化；outbox user_rows_purge 重放）──
    # 四表全归本域。原实现是 mgmt 侧在**同一个事务**里逐表 DELETE，兜底 SQL 才是
    # 跨域统计来源；三库拓扑下兜底侧打 mgmt 进程库必 1146（视图 0）。
    # 语句与改造前兜底 SQL 逐字一致（表名/谓词不变），只换 `?` 占位符。
    "user_purge_agent_tasks": "DELETE FROM agent_tasks WHERE user_id=?",
    "user_purge_chat_agent_session": "DELETE FROM chat_agent_session WHERE user_id=?",
    "user_purge_chat_conversations": "DELETE FROM chat_conversations WHERE user_id=?",
    "user_purge_issue": "DELETE FROM issue WHERE user_id=?",
    'agent_runtime_engine_uid_by_id': 'SELECT engine_uid FROM agent_runtimes WHERE id=?',
    'agent_runtime_snapshot_by_id': 'SELECT engine_uid, COALESCE(display_name, name), name FROM agent_runtimes WHERE id=?',
    'devops_agent_session_insert_uid': "INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, engine_uid, skill, skill_id, repo_gitcode_token, status, enable, subscribe_events, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
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
        "last_ip, repo_key, org_id, spec, cred_type, platform FROM runtime_credentials"),
    "runtime_credentials_active_ids": (
        "SELECT id FROM runtime_credentials WHERE status='active' ORDER BY id DESC"),
    "runtime_credentials_list": (
        "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, "
        "created_at, repo_key FROM runtime_credentials ORDER BY id DESC"),
    "runtime_credentials_by_org": (
        "SELECT id, name, device_name, scope, owner_id, owner_name, status, last_seen_at, "
        "created_at, repo_key FROM runtime_credentials WHERE org_id=? ORDER BY id DESC"),
    "runtime_credential_exists": "SELECT id FROM runtime_credentials WHERE id=?",
    # 本机指标上报（api/server_tasks.py 的 insight 进程 15s 心跳）：
    # 原实现在 insight 进程库里直查/直写 runtime 表 → 三库拓扑 1146 被 except 吞成
    # 「register/upload failed」，本机 device 常量从未登记也从未刷新。
    "runtime_credential_device_id": (
        "SELECT id FROM runtime_credentials WHERE device_name=? AND scope='global'"),
    "runtime_credential_register_device": (
        "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, last_ip) "
        "VALUES (?, ?, ?, 'global', 'active', ?)"),
    "runtime_credential_touch_device": (
        "UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, "
        "memory_percent=?, disk_percent=?, tmp_percent=?, bandwidth_percent=?, "
        "bandwidth_mbps=?, load_avg=? WHERE device_name=? AND scope='global'"),
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
    "skill_official_summary_rows": (
        "SELECT id, skill_name, description, summary FROM skill "
        "WHERE is_active=1 AND scope='official' ORDER BY agent_type, skill_name"),
    "skill_detail_by_id": (
        "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id, "
        "is_featured, like_count, sub_scene, category_id, cat_l1_id, cat_l2_id, tags "
        "FROM skill WHERE id=? AND is_active=1"),
    "skill_detail_by_name": (
        "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id, "
        "is_featured, like_count, sub_scene, category_id, cat_l1_id, cat_l2_id, tags "
        "FROM skill WHERE skill_name=? AND is_active=1 "
        "AND (scope='official' OR (scope='user' AND owner_id=?) OR scope='repo') "
        "ORDER BY CASE scope WHEN 'user' THEN 0 WHEN 'repo' THEN 1 ELSE 2 END, id DESC LIMIT 1"),
    "skill_path_owner_scope": (
        "SELECT skill_name, owner_id, skill_path, scope FROM skill WHERE id=? AND is_active=1"),
    "skill_meta_row": (
        "SELECT skill_name, scope, owner_id, agent_type, category_id, "
        "cat_l1_id, cat_l2_id, tags FROM skill WHERE id=? AND is_active=1"),
    "skill_name_path": "SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1",
    "skill_repo_full_row": (
        "SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id "
        "FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 LIMIT 1"),
    "skill_name_path_scope": (
        "SELECT skill_name, skill_path, scope FROM skill WHERE id=? AND is_active=1"),
    # ── 技能显示名解析（src/api/skill_display.py 收口，Issue #980）──
    # 调用方按回退链逐层查；列只有 display_name，列序与原 SQL 逐字一致。
    # user/repo 两档在原实现里是两条字面量 SQL，档位只是谓词值，合成一条参数化语句。
    "skill_display_name_by_id": (
        "SELECT display_name FROM skill WHERE id=? AND is_active=1"),
    "skill_display_name_by_scope": (
        "SELECT display_name FROM skill WHERE skill_name=? AND scope=? AND owner_id=? "
        "AND is_active=1 LIMIT 1"),
    "skill_active_display_rows": (
        "SELECT skill_name, display_name, scope, owner_id FROM skill WHERE is_active=1"),
    # ── 场景节点技能 scope 判定（src/api/scene_skill_scope.py）读路径收口 ──────
    # 原实现在 api 侧进程主库里直查 skill（归本域）：三库拓扑下 api 进程库没有该表
    # （视图 0 → 1146），两处调用都被 except 吞成「空 agent_type / 无改绑建议」。
    # 列序、谓词、CASE 优先级与原 SQL 逐字一致；`LIMIT ?` 绑定参数（SQLite / MySQL 8
    # 均支持绑定 LIMIT），不使用 MySQL 专有语法。
    "skill_agent_type_by_priority": (
        "SELECT agent_type FROM skill WHERE skill_name=? AND is_active=1 "
        "ORDER BY CASE WHEN scope='repo' AND owner_id=? THEN 0 "
        "WHEN scope='official' THEN 1 WHEN scope='repo' THEN 2 ELSE 3 END "
        "LIMIT 1"),
    "skill_rebind_candidates": (
        "SELECT id, skill_name, scope, agent_type FROM skill "
        "WHERE is_active=1 AND skill_name<>? AND "
        "((scope='repo' AND owner_id=?) OR scope='official') "
        "ORDER BY CASE scope WHEN 'repo' THEN 0 ELSE 1 END, skill_name LIMIT ?"),
    "skill_rebind_candidates_typed": (
        "SELECT id, skill_name, scope, agent_type FROM skill "
        "WHERE is_active=1 AND skill_name<>? AND "
        "((scope='repo' AND owner_id=?) OR scope='official') AND agent_type=? "
        "ORDER BY CASE scope WHEN 'repo' THEN 0 ELSE 1 END, skill_name LIMIT ?"),
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
    # 用量台账归属反查（insight/observability/llm_usage）：列序与调用方解包顺序逐字一致，
    # 不复制 insight 侧的任何解析口径（scene 归因留在调用方）。
    "task_attribution_by_id": (
        "SELECT repo_key, agent_type, runtime_id, user_id, issue_id "
        "FROM agent_tasks WHERE id=?"),
    "devops_session_runtime_id_by_repo_agent": (
        "SELECT runtime_id FROM devops_agent_session "
        "WHERE repo_key=? AND agent_type=? AND runtime_id>0 LIMIT 1"),
    # agent_runtimes 与 runtime_credentials 同属 runtime 域，JOIN 留在本域内
    "runtime_engine_attribution": (
        "SELECT ar.name, ar.credential_id, COALESCE(rc.device_name,''), "
        "COALESCE(rc.last_ip,''), COALESCE(ar.provider,'') "
        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc "
        "ON rc.id=ar.credential_id WHERE ar.id=?"),
    # 配置模型回落（#865 老库可能无 model_name 列）：存在性探测在 owner 侧连接上做，
    # 调用方只拿「模型名或空串」，不再自己探测本域表结构。
    "agent_runtimes_model_name": "SELECT model_name FROM agent_runtimes WHERE id=?",
    # 巡检（api/patrol.py）读路径：issue / events / agent_tasks / agent_runtimes
    "issue_status_by_repo_number": (
        "SELECT status FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1"),
    "issue_stage_by_repo_number": (
        "SELECT stage FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1"),
    "task_event_seq": "SELECT event_seq FROM agent_tasks WHERE id=? LIMIT 1",
    # Issue #2327：dispatch_stalled 巡检候选（谓词/列序照抄原 api 侧 SQL，
    # 三库拓扑下由 _any_cursor 路由到 runtime 域；查询已按状态/语义收窄、
    # 不再用 LIMIT 截断，避免 7 天窗内早于上限的卡死事件被漏扫）。
    # 形态一：running + runtime 空 + 无任何 agent_tasks 行（真「没派出去」；
    # 有任务行的广播池排队属正常容量等待，不报）。
    "patrol_a2a_unclaimed_rows": (
        "SELECT e.seq, e.repo_key, e.issue_id, e.agent_type, e.status, e.runtime, "
        "e.result, e.payload, e.created_at, e.started_at, e.completed_at "
        "FROM events e LEFT JOIN agent_tasks t ON t.event_seq = e.seq "
        "WHERE e.event_type='a2a_event' AND e.status='running' "
        "AND (e.runtime IS NULL OR e.runtime='') AND t.id IS NULL "
        "ORDER BY e.seq DESC"),
    # 形态二/三：failed 且失败语义=派发失败/排队超时（结果文本已含
    # _fail_event 写入的 [code]；LIKE 预筛只用于减少跨库取数，最终归类仍在 patrol）。
    "patrol_a2a_failed_dispatch_rows": (
        "SELECT seq, repo_key, issue_id, agent_type, status, runtime, result, "
        "payload, created_at, started_at, completed_at FROM events "
        "WHERE event_type='a2a_event' AND status='failed' AND ("
        "result LIKE '%[dispatch_failed]%' OR result LIKE '%[a2a_dispatch_failed]%' "
        "OR result LIKE '%[a2a_dispatch_exception]%' "
        "OR result LIKE '%[a2a_queue_dispatch_failed]%' "
        "OR result LIKE '%[a2a_queue_dispatch_exception]%' "
        "OR result LIKE '%排队超时%' OR result LIKE '%队列超时%' "
        "OR result LIKE '%未被认领%' OR result LIKE '%queue_timeout%' "
        "OR result LIKE '%unclaimed%') "
        "ORDER BY seq DESC"),
    # 补偿终检读原任务终态（api/patrol_compensate.py RecheckHandler）：列序与原查询逐字一致
    "task_recheck_row": (
        "SELECT completed_at, agent_type, result_text FROM agent_tasks WHERE id=?"),
    "event_payload_status_a2a": (
        "SELECT payload, status FROM events WHERE repo_key=? AND issue_id=? "
        "AND event_type='a2a_event'"),
    "event_a2a_created_agent": (
        # Issue #2118 复审：failed 的 a2a_event 不算「已交棒证据」——自动推进/重排
        # 事件派发失败时仍会被巡检当成交棒成功，导致真断链被漏报、Issue 静默停链。
        "SELECT created_at, agent_type FROM events WHERE repo_key=? AND issue_id=? "
        "AND event_type='a2a_event' AND COALESCE(status,'') != 'failed'"),
    # A2A 派发门禁（api/a2a_dispatch_guard.py）读路径：events / event_skipped 归本域。
    # 列序与原 SQL 逐字一致（调用方按下标解包）；谓词/排序/条数上限照抄原语句。
    "a2a_window_dedup_rows": (
        "SELECT seq, created_at, status, scene_id FROM events "
        "WHERE repo_key=? AND issue_id=? AND event_type='a2a_event' "
        "AND agent_type=? ORDER BY seq DESC LIMIT 20"),
    "a2a_storm_event_times": (
        "SELECT created_at FROM events WHERE repo_key=? AND issue_id=? "
        "AND event_type='a2a_event' AND agent_type=? ORDER BY seq DESC LIMIT 50"),
    "a2a_storm_skip_rows": (
        "SELECT created_at, detail FROM event_skipped WHERE repo_key=? "
        "AND issue_id=? AND event_type='a2a_event' ORDER BY id DESC LIMIT 100"),
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
    # api server_common 运维诊断（/healthz 表探活 + /health/realtime 全量统计）
    # 原 SQL 在 api 进程主库直发，三库拓扑下 api 库无 agent_tasks/agent_runtimes → 恒失败。
    "health_task_rows_total": "SELECT COUNT(*) FROM agent_tasks",
    "health_task_status_counts": "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status",
    "health_task_inflight_count": (
        "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('dispatched','running')"),
    "health_runtime_rows": "SELECT status, last_seen_at, credential_id FROM agent_runtimes",
    # 业务采样器按 source 分类计数（insight/observability/__init__.collect_business_sampler）
    # 列序/谓词与原 insight 侧语句逐字一致：COALESCE(agent_type,'unknown') + COUNT(*) 分组。
    "obs_sampler_task_queued": (
        "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
        "WHERE status='queued' GROUP BY agent_type"),
    "obs_sampler_task_running": (
        "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
        "WHERE status IN ('dispatched','running') GROUP BY agent_type"),
    "obs_sampler_task_stuck": (
        "SELECT COALESCE(agent_type,'unknown'), COUNT(*) FROM agent_tasks "
        "WHERE status='running' AND started_at IS NOT NULL AND started_at < ? "
        "GROUP BY agent_type"),
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
    "skill_category_tags_update": (
        "UPDATE skill SET cat_l1_id=?, cat_l2_id=?, tags=?, updated_at=datetime('now') "
        "WHERE id=?"),
    "skill_row_create_ignore": (
        "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, "
        "skill_path, scope, owner_id, created_by, updated_by) VALUES (?,?,?,?,?,?,?,?,?)"),
    "skill_like_inc": "UPDATE skill SET like_count=like_count+1 WHERE id=?",
    "skill_like_dec": "UPDATE skill SET like_count=like_count-1 WHERE id=?",
    # Issue #3168 技能详情页：版本 / 文件清单 / 评论 / 下载计数
    "skill_detail_extra_by_id": (
        "SELECT summary, download_count, created_at, updated_at, created_by, updated_by "
        "FROM skill WHERE id=?"),
    "skill_summary_update": "UPDATE skill SET summary=?, updated_at=datetime('now') WHERE id=?",
    "skill_meta_patch_update": (
        "UPDATE skill SET description=?, agent_type=?, category_id=?, cat_l1_id=?, "
        "cat_l2_id=?, tags=?, summary=?, updated_by=?, updated_at=datetime('now') "
        "WHERE id=?"),
    "skill_download_inc": (
        "UPDATE skill SET download_count=COALESCE(download_count,0)+1 WHERE id=?"),
    "skill_intro_override_update": (
        "UPDATE skill SET intro_override=?, updated_at=datetime('now') WHERE id=?"),
    "skill_intro_override_by_id": (
        "SELECT intro_override FROM skill WHERE id=?"),
    "skill_version_latest": (
        "SELECT version, uploaded_at, uploaded_by, change_note FROM grape_t_skill_version "
        "WHERE skill_id=? ORDER BY uploaded_at DESC, id DESC LIMIT 1"),
    "skill_versions_by_skill": (
        "SELECT version, uploaded_at, uploaded_by, change_note FROM grape_t_skill_version "
        "WHERE skill_id=? ORDER BY uploaded_at DESC, id DESC"),
    "skill_version_insert": (
        "INSERT INTO grape_t_skill_version (id, skill_id, version, change_note, uploaded_at, uploaded_by) "
        "VALUES (?,?,?,?,?,?)"),
    "skill_file_delete_by_skill": "DELETE FROM grape_t_skill_file WHERE skill_id=?",
    "skill_file_insert": (
        "INSERT INTO grape_t_skill_file (id, skill_id, path, size) VALUES (?,?,?,?)"),
    "skill_files_by_skill": (
        "SELECT path, size FROM grape_t_skill_file WHERE skill_id=? ORDER BY path"),
    "skill_file_count": "SELECT COUNT(*) FROM grape_t_skill_file WHERE skill_id=?",
    "skill_comment_count": "SELECT COUNT(*) FROM grape_t_skill_comment WHERE skill_id=?",
    "skill_comments_page": (
        "SELECT id, user_id, user_name, content, created_at FROM grape_t_skill_comment "
        "WHERE skill_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"),
    "skill_comment_insert": (
        "INSERT INTO grape_t_skill_comment (id, skill_id, user_id, user_name, content, created_at) "
        "VALUES (?,?,?,?,?,?)"),
    # 相关技能候选池：热度前 N（与 mgmt 路由 _RELATED_POOL_SIZE 同值；QA B2：数值须具名）
    "skills_related_pool": (
        "SELECT id, skill_name, display_name, description, agent_type, scope, owner_id, tags, "
        "like_count, category_id, summary FROM skill "
        "WHERE is_active=1 AND id<>? ORDER BY like_count DESC, id DESC LIMIT "
        + str(_SKILL_RELATED_POOL_SIZE) + ""),
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
    # 单语句 upsert（2026-11-21）：存在性由主键回答，不靠 UPDATE 的 rowcount。
    # MySQL 的 UPDATE 返回「实际改变的行数」，同值重写 = 0 → 旧的两段式会误判
    # 「这行不存在」再去 INSERT → 1062 meta.PRIMARY（实测：每次启动刷一条）。
    # ON CONFLICT(key) 由方言层翻译成 ON DUPLICATE KEY UPDATE（mysql_compat）。
    # `key` 是 MySQL 保留字，列名必须加反引号（否则 1064 near '`key` UPDATE …'）；
    # 反引号在 SQLite 侧同样合法，一套 SQL 两边都能跑。
    "meta_upsert": "INSERT INTO meta (`key`, value) VALUES (?, ?) "
                   "ON CONFLICT(`key`) DO UPDATE SET value=excluded.value",
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
    # agent_tasks / events / runtime_credentials 三张表全归本域（table_ownership.json）。
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
    # ── 存量场景绑定修复（Issue #541；api/scene_binding_repair.py 一次性迁移）──
    # issue 归本域；原实现在 api 进程主库直发 → 三库拆分后 1146，修复扫描静默归零。
    # 列序/谓词逐字照抄；写语句走 CAS（scene_id 仍是旧值才改），与原
    # `UPDATE ... WHERE id=? AND scene_id=?` 同一并发口径。
    "issue_bound_scene_rows": (
        "SELECT id, repo_key, issue_number, scene_id FROM issue WHERE scene_id != ''"),
    "issue_scene_set_cas": "UPDATE issue SET scene_id=? WHERE id=? AND scene_id=?",
    # ── Issue #995 VCS 编号索引（api/issue_vcs_index.py 台账读写）─────────────
    # issue 归本域；原实现在 api 进程主库直发 → 三库拆分后 1146：存在性回写整批
    # 失败（映射 unavailable）、存量回扫归零，过滤与回填在独立拓扑下静默失效。
    # 列序/谓词逐字照抄（调用方按下标取值）。
    "issue_vcs_index_rows": (
        "SELECT id, issue_number, vcs_exists, vcs_checked_at FROM issue WHERE repo_key=?"),
    "issue_vcs_backfill_repos": (
        "SELECT DISTINCT repo_key FROM issue "
        "WHERE vcs_checked_at IS NULL OR vcs_checked_at=''"),
    "issue_vcs_checked_count": (
        "SELECT COUNT(*) FROM issue WHERE repo_key=? AND vcs_checked_at IS NOT NULL"),
    # ── insight 数据缺失探测（observability/gap.py）跨域读口 ────────────────
    # runtime_credentials / agent_tasks 归本域；原实现在 insight 进程库直发 → 三库
    # 拆分后 1146，被 except 吞成「无静默 / 无缺失」——数据缺失探测整体失效。
    # 列序/谓词逐字照抄（调用方按下标解包）；两处 runtime_credentials 列集不同，
    # 各自保留一条（不并成超集，避免改动调用方解包顺序）。
    "runtime_credentials_gap_rows": (
        "SELECT id, device_name, name, last_seen_at, repo_key, org_id, scope"
        " FROM runtime_credentials WHERE status != 'revoked'"),
    "runtime_credentials_silent_rows": (
        "SELECT id, name, device_name, scope, owner_name, org_id, last_seen_at,"
        " status, repo_key FROM runtime_credentials WHERE status != 'revoked'"),
    "agent_task_activity_rows": (
        "SELECT repo_key, created_at, started_at, completed_at FROM agent_tasks"
        " WHERE repo_key IS NOT NULL AND repo_key != ''"),
    # ── 全局搜索（insight/routes/search.py 跨域收口）────────────────────────
    # events / agent_type 归本域；原实现取 insight 路由文件里的进程主库游标直发 →
    # 三库拆分后 1146，被两条 `except` 吞成空列表，搜索结果「事件/智能体」静默恒空。
    # 列序/谓词逐字照抄；两条都**不按仓库过滤**、也不过滤 `enable`：
    # 原 SQL 的 LIMIT 10 先于调用方 Python 侧的「可见仓库」过滤生效，把仓库条件
    # 下推到 SQL 会改变结果集（多取被过滤掉的行），故过滤与权限判定仍留调用方。
    "events_search_global": (
        "SELECT seq, repo_key, event_type, title, status FROM events"
        " WHERE LOWER(title) LIKE ? OR LOWER(event_type) LIKE ? OR LOWER(repo_key) LIKE ?"
        " ORDER BY seq DESC LIMIT 10"),
    "agent_type_search_global": (
        "SELECT agent_type, display_name, icon FROM agent_type"
        " WHERE LOWER(agent_type) LIKE ? OR LOWER(display_name) LIKE ?"),
    # ── 设备资源曲线采样基集（insight/observability/rollup.py）──
    # `_READ_QUERIES` 里同名键是 `%s` 方言的 lib 专用版；这里登记**可移植**同名语句
    # （零参数、任意拓扑）。JOIN 的两张表都归本域，故 JOIN 留在本域、不需要 Python 内连接。
    "runtime_credentials_619821": (
        "SELECT rc.id, rc.device_name, rc.cpu_percent, rc.memory_percent, rc.disk_percent,"
        " rc.bandwidth_percent, COALESCE(hs.health_score, 0), rc.status, rc.last_seen_at"
        " FROM runtime_credentials rc"
        " LEFT JOIN runtime_health_scores hs ON hs.runtime_id = rc.id"
        " WHERE rc.status != 'revoked'"),
    # ── QA 结论门禁读口（api/qa_gate.py）─────────────────────────────────────
    # 原实现在 api 进程主库上拼 `{structured_col}` 与场景谓词后直发 events；三库拆分后
    # api 库没有 events（视图 0 → 1146），`latest_qa_event` 恒 None → 门禁整体 fail-open
    # 失效（qa 失败也能关闭/合入）。两个动态维度（列集 / 场景回退）各登记一条语句，
    # 调用方不再拼 SQL；列序与原查询逐字一致（调用方按下标解包）。
    "qa_gate_latest_event_by_scene": (
        "SELECT seq, status, result, summary, structured_output FROM events "
        "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
        "AND scene_id=? ORDER BY seq DESC LIMIT 1"),
    "qa_gate_latest_event_by_scene_text": (
        "SELECT seq, status, result, summary FROM events "
        "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
        "AND scene_id=? ORDER BY seq DESC LIMIT 1"),
    # 仓库默认/未绑定场景：历史事件的 scene_id 可能是空串或 NULL，一并命中。
    "qa_gate_latest_event_default_scene": (
        "SELECT seq, status, result, summary, structured_output FROM events "
        "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
        "AND (scene_id=? OR scene_id='' OR scene_id IS NULL) "
        "ORDER BY seq DESC LIMIT 1"),
    "qa_gate_latest_event_default_scene_text": (
        "SELECT seq, status, result, summary FROM events "
        "WHERE repo_key=? AND issue_id IN (?, ?) AND agent_type='qa' "
        "AND (scene_id=? OR scene_id='' OR scene_id IS NULL) "
        "ORDER BY seq DESC LIMIT 1"),
    # 关联 PR 反查：payload 原值返回（JSON 解析与编号提取口径仍在调用方）。
    "qa_gate_latest_pr_payload": (
        "SELECT payload FROM events WHERE repo_key=? AND issue_id IN (?, ?) "
        "AND event_type IN ('note_hook_pr','pull_request_opened',"
        "'pull_request_updated','pull_request_merged') "
        "ORDER BY seq DESC LIMIT 1"),
    # ── 接管会话状态（api/git_repo_api.py 的 tko_ 二次校验）──
    # 列序 / 排序 / LIMIT 与原语句逐字一致：只取 status 一列，按最近一条会话判定。
    "takeover_session_status_by_task": (
        "SELECT status FROM takeover_sessions WHERE task_id=? "
        "ORDER BY created_at DESC LIMIT 1"),
    # 接管会话心跳（src/domains/mgmt/routes/takeover.py 的 progress 端点）：
    # 列名与 WHERE 逐字照抄原直发句，只按主键覆写 updated_at。
    "takeover_touch_updated_at": (
        "UPDATE takeover_sessions SET updated_at=? WHERE id=?"),
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


@contextmanager
def _owner_cursor(cursor: Any = None):
    """能效看板取数游标：单库/测试拓扑复用调用方游标，自有 DSN 时忽略它。

    能效看板模块（insight/observability/efficiency）的设计契约是「喂一个 cursor +
    SQLite 建表即可单测」。单库拓扑下调用方游标与进程主库本就是同一个库，复用它省
    一次嵌套加锁并保住调用方的事务快照；三库拓扑（本域有自有 DSN）下调用方游标指向
    insight 进程库（没有本域表），必须忽略 —— 那正是原来 1146 的来源。
    """
    if cursor is not None and not db_registry.uses_own_dsn("runtime"):
        yield cursor
        return
    with _any_cursor() as c:
        yield c


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


def patrol_dispatch_stall_threshold_seconds() -> int:
    """派发静默窗默认值的单一来源（Issue #2327）。

    复用平台既有 queued 老化窗 `engine/service._STALE_QUEUED_THRESHOLD`
    （env `HEALTH_STALE_QUEUED_THRESHOLD`，默认 120s，Issue #134）——
    该常量已定义「queued 任务超龄即视为卡住」的项目口径，巡检的
    dispatch_stalled 静默窗与它同语义，故不再另立魔数。
    """
    from src.domains.runtime.engine.service import _STALE_QUEUED_THRESHOLD
    return int(_STALE_QUEUED_THRESHOLD)


def exec_any(name: str, params: tuple = ()) -> int:
    """按具名查询执行写语句（任意拓扑），返回影响行数。"""
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql(name), params)
        return int(getattr(cur, "rowcount", 0) or 0)


# ── QA 结论门禁（api/qa_gate.py）读口 ──────────────────────────────────────
# 只做「取数 + 旧库列回退」，事件语义（seq 最大、结论文本判定、fail-open）仍留调用方。

def qa_gate_latest_event_any(repo_key: str, issue_tags, scene_id: str,
                            default_scene: bool = False) -> tuple[Any, ...] | None:
    """该 Issue 同一流水线最新一条 qa 事件元组（5 列），无记录/异常回 None。

    列序：``seq, status, result, summary, structured_output``（与改造前逐字一致）。
    ``default_scene=True`` 用于仓库默认/未绑定场景：scene_id 为空串或 NULL 的历史
    事件同样命中（判定口径属调用方的流水线解析，本函数只按布尔选择语句）。
    旧库无 ``structured_output`` 列时回退 4 列版本，structured 位置由调用方按长度判断。
    """
    _name = ("qa_gate_latest_event_default_scene" if default_scene
             else "qa_gate_latest_event_by_scene")
    _params = (repo_key, issue_tags[0], issue_tags[1], scene_id)
    try:
        _row = read_one_any(_name, _params)
    except Exception as _e:
        # 旧库（迁移前的 events）无 structured_output 列：退回纯文本四列判定。
        log.debug(f"qa gate: structured_output column unavailable, use text columns: {_e}")
        _row = read_one_any(_name + "_text", _params)
    return tuple(_row) if _row is not None else None


def qa_gate_latest_pr_payload_any(repo_key: str, issue_tags) -> Any | None:
    """该 Issue 最近一条 PR 类事件的 payload 原值（不解析 JSON）；无记录回 None。"""
    _row = read_one_any("qa_gate_latest_pr_payload",
                        (repo_key, issue_tags[0], issue_tags[1]))
    return _row[0] if _row else None


# ── 接管会话状态（api/git_repo_api.py 的 tko_ 二次校验）──
# 原实现拿接管模块自己的 `db_lock` + `db.cursor()` 直查本域表：单库拓扑下就是进程
# 主库，三库拆分后 api 进程库没有该表（视图 0 → 1146），校验落到 `except → 503`
# 把**所有**接管态仓库操作打成 503。下面这条只按 task_id 取最近一条会话的 status；
# 「active 才放行」的状态机判定与 403/503 文案仍留调用方。

def takeover_session_status_any(task_id: str) -> str | None:
    """该任务最近一条接管会话的状态（任意拓扑）；无行回 None。

    列序与原语句逐字一致（只取 ``status``），按 ``created_at DESC`` 取第一条；
    不过滤 ``status`` 也不按 token 归属过滤 —— 原实现的判定口径就这样，本函数只换通道。
    """
    _task = "" if task_id is None else str(task_id)
    _row = read_one_any("takeover_session_status_by_task", (_task,))
    return _row[0] if _row else None


# ── 接管会话心跳（src/domains/mgmt/routes/takeover.py 的 progress 端点）──
# 原实现在 mgmt 路由里拿接管引擎的 `svc.db_lock` + `svc.db.cursor()` 直发本域表：
# 单库拓扑下那是进程主库（能写通），三库拆分后 mgmt 进程库没有本表（视图 0 → 1146），
# 心跳被 `except → log.warning` 吞掉，面板「最后活动」恒停在 progress 之外的时间点。
# 语句上移本域后两条拓扑跑同一份 SQL，路由只留「失败不阻断进展上报」的业务判断。

def takeover_touch_updated_at_any(session_id: str, updated_at: str) -> int:
    """按会话 id 覆写 ``updated_at``（心跳），返回影响行数（0 = 该行不存在）。

    语义逐字照抄原直发句：
      - ``updated_at`` 写调用方算好的时间串（**不改成 NOW()**：两条后端口径一致）；
      - ``WHERE id=?`` 只命中一行，不改状态 / ``active_key`` / 其它列，无行时返回 0
        （不插入、不兜底造行）；
      - 不做 ``is_active`` 之类的业务过滤 —— 已结束会话的心跳同样落库，与原实现一致。
    通道见 ``_any_cursor(write=True)``：自有 DSN → 本域连接（autocommit，立即生效）；
    单库 / 测试 → 进程主库 + 本域 ``db_lock`` 并提交，等价于调用方原来的
    ``with db_lock: ...; db.commit()``。提交责任在本函数内闭合，因为原调用点也是
    独立于进展写回的一次提交（两段事务，不合并）。
    """
    _sid = "" if session_id is None else str(session_id)
    _ts = "" if updated_at is None else str(updated_at)
    with _any_cursor(write=True) as c:
        cur = c.execute(_any_sql("takeover_touch_updated_at"), (_ts, _sid))
        return int(getattr(cur, "rowcount", 0) or 0)


# ── insight 数据缺失探测（observability/gap.py）的基集读口 ──
# 原实现用 insight 进程库游标直查本域两张表：三库拆分后 1146 → 调用方 except 把
# 异常吞成空列表，探测恒报「无静默」。三个函数只回元组原值，静默判定/退役窗口/
# 事件生命周期仍留调用方（那是 insight 的口径，不属于本域）。

def runtime_credentials_gap_rows_any() -> list[tuple]:
    """未注销凭据的静默探测基集（7 列，列序与 insight 原 SQL 逐字一致）。"""
    return [tuple(r) for r in read_all_any("runtime_credentials_gap_rows")]


def runtime_credentials_silent_rows_any() -> list[tuple]:
    """未注销凭据的 L2 静默名单基集（9 列，含 status / owner_name / scope）。"""
    return [tuple(r) for r in read_all_any("runtime_credentials_silent_rows")]


def agent_task_activity_rows_any() -> list[tuple]:
    """有仓库归属的任务活动行 `(repo_key, created_at, started_at, completed_at)`。"""
    return [tuple(r) for r in read_all_any("agent_task_activity_rows")]


# ── 免鉴权健康端点的运行时就绪快照（insight/routes/health.py）──
# 原实现两条语句跑在**同一个**进程主库游标上（先取旗舰在线行、再取总数），且在 lib
# 可用分支上把已 materialize 的行元组又 `.fetchone()` 了一次 —— 三库拓扑下必
# AttributeError，被 `except: pass` 吞成 `agent_runtime="unknown"`。本函数用一条
# `_any_cursor()` 保留「同游标两查」的顺序语义（两次连接会看到不同快照），
# 并把 SQL 收在本域；`online/missing/unhealthy` 的命名是端点的展示口径，留调用方。

def agent_runtime_health_snapshot_any() -> tuple[Any | None, int]:
    """运行时健康快照：`(在线且健康的旗舰引擎行, runtime 总行数)`。

    旗舰行是 `(name, status, healthy)` 三列元组（列序同原 SQL），无则 `None`；
    按 `opencode → codearts → 其他` 的引擎优先级取第一行（同原 SQL 的 CASE 排序）。
    第二查只在没有旗舰行时才有意义，但为保持与原实现的语句序一致仍然执行。
    返回整行而非布尔：`online/missing/unhealthy` 的状态命名属端点展示口径，留调用方。
    """
    with _any_cursor() as c:
        c.execute(_any_sql("agent_runtimes_34b97d"))
        ready_row = c.fetchone()
        c.execute(_any_sql("agent_runtimes_total_count"))
        count_row = c.fetchone()
    total = int(count_row[0] or 0) if count_row else 0
    return (ready_row, total)


# ── 设备资源曲线采样基集（insight/observability/rollup.py）──
# 原实现「lib 可用 → lib；否则 → 进程主库兜底 SQL」；三库拓扑下 insight 进程库没有
# 本域两张表（视图 0）→ 1146，被同一函数的外层 except 吞成 `{"inserted": 0}`，
# 资源曲线整条为空且无日志指向根因。本原语把 SQL 收在本域，两条拓扑同一份语句。

def runtime_credential_sample_rows_any() -> list[tuple]:
    """未注销凭据的资源采样基集（9 列，列序与 insight 原 SQL 逐字一致）。

    列为 `(id, device_name, cpu_percent, memory_percent, disk_percent,
    bandwidth_percent, health_score, status, last_seen_at)`；`health_score` 由
    LEFT JOIN 取得、缺行时 `COALESCE` 成 0（同原 SQL）。在线判定、空值兜底与
    采样落库都是 insight 的口径，留调用方。
    """
    return [tuple(r) for r in read_all_any("runtime_credentials_619821")]


# ── 飞书扫码 onboarding：换绑清理的三个读写原语（mgmt/routes/feishu_onboard.py）──
# 三张表（chat_conversations / vod_conversations / devops_agent_session）全归本域。
# 原调用点「lib 可用 → lib；否则 → 进程主库兜底 SQL」，本批把兜底侧收成任意拓扑原语：
# 自有 DSN 走 db_registry，单库/测试拓扑回落进程库（`_any_cursor` 自带 db_lock，故调用方
# 不再自己持锁 —— 避免与测试里 monkeypatch 的裸 Lock 嵌套自死锁）。

def vod_session_id_by_repo_any(repo_key: str) -> str:
    """某仓库的 vod agent 会话 id（无则空串）；谓词与列同 mgmt 侧原查询。"""
    if not repo_key:
        return ""
    row = read_one_any("devops_session_vod_id", (str(repo_key),))
    return str(row[0] or "") if row else ""


def chat_conversations_delete_by_im_channel_any(im_channel_id: str) -> int:
    """按 IM 渠道清空 chat 会话（换绑/解绑清理），返回删除行数；空渠道名零 SQL。"""
    if not im_channel_id:
        return 0
    return exec_any("conversation_delete_by_im_channel", (str(im_channel_id),))


def vod_conversation_delete_by_session_any(agent_session_id: str) -> int:
    """按 agent_session_id 删 VOD 会话，返回删除行数；空 id 零 SQL。"""
    if not agent_session_id:
        return 0
    return exec_any("vod_conversation_delete_by_session", (str(agent_session_id),))


def vod_agent_session_ensure_any(session_id: str, repo_key: str, status: str = "idle") -> int:
    """幂等建 vod agent 会话行（已存在则不动），返回 1=新建 / 0=已存在。

    与 lib 侧 `ensure_agent_session` 同语义（INSERT IGNORE）：换绑流程可重试，
    两拓扑不会一边幂等、一边抛 (repo_key, agent_type) 唯一键冲突。
    """
    if not session_id or not repo_key:
        return 0
    return exec_any("devops_agent_session_insert_vod",
                    (str(session_id), str(repo_key), status))


# ── 存量场景绑定修复（Issue #541；api 一次性迁移）──────────────────────
# 原实现从 api 进程主库直发 issue 的两条 SQL；三库拆分后 api 进程库没有 issue
# （视图 0）→ 扫描/改绑全被 except 吞成「0 修复」。issue 是本域表，SQL 只留这里。

def issue_bound_scene_rows_any() -> list[tuple]:
    """所有已绑定场景的 issue (id, repo_key, issue_number, scene_id)；空串绑定不计。"""
    return [tuple(r) for r in read_all_any("issue_bound_scene_rows", ())]


def set_issue_scene_cas_any(issue_id, old_scene: str, new_scene: str) -> int:
    """仅当 scene_id 仍是旧值时才改绑（CAS），返回影响行数；并发已改走返回 0。"""
    return exec_any("issue_scene_set_cas", (new_scene, issue_id, old_scene))


# ── Issue #995 VCS 编号索引的存在性台账读写（api/issue_vcs_index.py）──────────
# 原实现从 api 进程主库直发 issue 的四条 SQL；三库拆分后 api 进程库没有 issue
# （视图 0）→ 读 1146 被吞成「回写失败 → unavailable」、回扫 1146 被吞成「0 仓」，
# VCS 存在性过滤与存量回填在独立拓扑下整体失效（卡片永不标删、过滤永不生效）。
# issue 是本域表，SQL 只留这里。
#
# `{ph}` 是 IN 列表占位符标记（按 id 个数生成 `?,?,...`）：语句文本固定在本模块，
# 调用方只能传 id 值列表 —— 参数化，不接受拼接 SQL，也不接受表名/列名入参。
# 读语句已登记在上方 `_ANY_QUERIES`（`read_one_any`/`read_all_any` 执行）。
# 时间列沿用 datetime('now')（mysql_compat 翻译成 CAST(NOW() AS CHAR)），与原实现
# 同一列同一表达式，不改「UTC 字符串」口径。
_ISSUE_VCS_QUERIES: dict[str, str] = {
    # 三态回写：标删 / 恢复 / 仅盖校验时间戳
    "issue_vcs_mark_deleted": (
        "UPDATE issue SET vcs_exists=0, vcs_checked_at=datetime('now') WHERE id IN ({ph})"),
    "issue_vcs_mark_restored": (
        "UPDATE issue SET vcs_exists=1, vcs_checked_at=datetime('now') WHERE id IN ({ph})"),
    "issue_vcs_stamp_checked": (
        "UPDATE issue SET vcs_checked_at=datetime('now') WHERE id IN ({ph})"),
}

# IN 列表单批上限：与原实现 `_chunks(size=500)` 逐字一致（超长语句/包体保护）。
_ISSUE_VCS_ID_CHUNK = 500


def _issue_vcs_in_stmt(name: str, ids: list) -> tuple[str, tuple]:
    """把 `{ph}` 展开成 len(ids) 个占位符，返回 (SQL, 参数元组)。"""
    sql = _ISSUE_VCS_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown issue vcs query: {name}")
    return sql.replace("{ph}", ",".join("?" * len(ids))), tuple(ids)


def issue_vcs_index_rows_any(repo_key: str) -> list[tuple]:
    """repo_key 下台账卡片 `(id, issue_number, vcs_exists, vcs_checked_at)`（无排序，同原语句）。"""
    return [tuple(r) for r in read_all_any("issue_vcs_index_rows", (repo_key,))]


def issue_vcs_backfill_repo_keys_any() -> list[str]:
    """尚待校验（vcs_checked_at 为空/NULL）的仓库集合，按 DISTINCT repo_key。"""
    return [r[0] for r in read_all_any("issue_vcs_backfill_repos", ()) if r[0]]


def issue_vcs_checked_count_any(repo_key: str) -> int:
    """该仓库已写入校验时间戳的卡片数（`vcs_checked_at IS NOT NULL`，空串仍计）。"""
    row = read_one_any("issue_vcs_checked_count", (repo_key,))
    return int(row[0] or 0) if row else 0


#: issue 按 id 批量取列的 IN 单批上限（SQLite 变量上限/包体保护，与既有分块查询同值）。
_ISSUE_ID_CHUNK = 500


def issue_stage_status_rows(ids, chunk: int = _ISSUE_ID_CHUNK) -> list[tuple]:
    """按 id 批量取 issue 的 `(id, stage, status)`，无过滤、无排序（同原 JOIN 右表取列）。

    供跨域 JOIN 拆分用（api/feedback_status.py：user_feedback ⋈ issue，右表归本域）。
    原语句对 issue 侧没有任何谓词，所以这里也不加（软删/子任务口径由调用方判定）；
    id 非法/为 None 的元素直接丢弃，空列表返回 []。分块只为占位符数量上限，
    行集合与一次 IN 等价。
    """
    want = [i for i in (ids or []) if i is not None]
    if not want:
        return []
    size = max(1, int(chunk or _ISSUE_ID_CHUNK))
    out: list[tuple] = []
    for i in range(0, len(want), size):
        part = want[i:i + size]
        ph = ",".join(["?"] * len(part))
        with _any_cursor() as c:
            c.execute(f"SELECT id, stage, status FROM issue WHERE id IN ({ph})",
                      tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


# ── 跨域 JOIN 拆分的右表读口（agent_tasks / runtime_credentials 归本域）──────
# 调用方是 insight 的 token 三级归因（observability/tokens.py）：原实现一条
# `obs_llm_usage LEFT JOIN agent_tasks t ON t.id = u.task_id`，外加一条
# `SELECT id, COALESCE(NULLIF(device_name,''), name, '') FROM runtime_credentials WHERE id IN (...)`。
# 两张表都归本域，三库独立后 insight 进程库里没有它们（视图 0）→ 1146 被调用方
# except 吞成「空窗口 / 无显示名」。语句文本固定在本模块（`{ph}` 只按 id 个数展开），
# 调用方只能传 id 值列表，不接受拼接 SQL / 表名 / 列名。

_TASK_SIDE_QUERIES: dict[str, str] = {
    "task_issue_ids": "SELECT id, issue_id FROM agent_tasks WHERE id IN ({ph})",
    "runtime_credential_display_names": (
        "SELECT id, COALESCE(NULLIF(device_name,''), name, '') FROM runtime_credentials "
        "WHERE id IN ({ph})"),
}


def _task_side_in_stmt(name: str, ids: list) -> tuple[str, tuple]:
    """把 `{ph}` 展开成 len(ids) 个占位符，返回 (SQL, 参数元组)。"""
    sql = _TASK_SIDE_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown task side query: {name}")
    return sql.replace("{ph}", ",".join("?" * len(ids))), tuple(ids)


def _task_side_in_rows(name: str, ids, chunk: int) -> list[tuple]:
    """按 `{ph}` 模板分块读，返回行元组列表（顺序 = 表序，不排序）。"""
    want: list = []
    seen: set = set()
    for i in (ids or []):
        if i is None or str(i) == "":
            continue
        key = str(i)
        if key in seen:
            continue
        seen.add(key)
        want.append(i)
    if not want:
        return []
    size = max(1, int(chunk or _ISSUE_ID_CHUNK))
    out: list[tuple] = []
    for i in range(0, len(want), size):
        sql, args = _task_side_in_stmt(name, want[i:i + size])
        with _any_cursor() as c:
            c.execute(sql, args)
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def task_issue_id_rows(ids, chunk: int = _ISSUE_ID_CHUNK) -> list[tuple]:
    """按任务 id 批量取 `(task_id, issue_id)`（供跨域 JOIN 拆分，右表 = agent_tasks）。

    列序与改造前宽 SQL 的 `t.issue_id` 侧一致；无谓词、无排序。任务行不存在 → 该 id
    无行（即 LEFT JOIN 的 NULL 侧，由调用方补 None）。id 为 None/空串的元素丢弃并去重，
    空列表返回 []。分块只为占位符数量上限，行集合与一次 IN 等价。
    """
    return _task_side_in_rows("task_issue_ids", ids, chunk)


def runtime_credential_display_names(ids, chunk: int = _ISSUE_ID_CHUNK) -> list[tuple]:
    """按凭据 id 批量取 `(id, 显示名)`；显示名 = `COALESCE(NULLIF(device_name,''), name, '')`。

    表达式与改造前逐字一致：device_name 为空/NULL 时退回 name，两者都空 → 空串。
    无排序（调用方按键取值，不依赖顺序）。分块与去重口径同 `task_issue_id_rows`。
    """
    return _task_side_in_rows("runtime_credential_display_names", ids, chunk)


# ── 流水线业务状态 / 阶段任务引用读口（api/pipeline_status.py）────────────────
# 原实现三条 SQL 从 api 进程主库直发，表 `issue` / `issue_state` / `agent_tasks`
# 三张全归本域。三库独立后 api 进程库里没有它们（视图 0）→ 1146 被调用方裸
# `except` 吞成「无业务状态 / 无任务引用」：流水线页的需求级 review_status 恒空、
# 每个段的 task_ref/result_ref 恒 unavailable（Level-1 展示整体失效）。
# 语句文本固定在本模块（`{repo_ph}` / `{seq_ph}` 只按值个数展开成占位符），
# 调用方只能传 repo_key / seq 值列表，不接受拼接 SQL / 表名 / 列名。
# 列序与原 SELECT 逐字一致；`created_at` 只为本域内部排序取用，不返回调用方。
_PIPELINE_STATUS_QUERIES: dict[str, str] = {
    "pipeline_issue_stage_status": (
        "SELECT stage, status FROM issue WHERE repo_key IN ({repo_ph}) AND issue_number=?"),
    "pipeline_issue_state_stage": (
        "SELECT stage FROM issue_state WHERE repo_key IN ({repo_ph}) AND issue_id=?"),
    "pipeline_segment_task_rows": (
        "SELECT id, event_seq, status, result_text, created_at FROM agent_tasks "
        "WHERE repo_key IN ({repo_ph}) AND event_seq IS NOT NULL "
        "ORDER BY created_at ASC, id ASC"),
    "pipeline_segment_task_rows_by_seq": (
        "SELECT id, event_seq, status, result_text, created_at FROM agent_tasks "
        "WHERE repo_key IN ({repo_ph}) AND event_seq IS NOT NULL AND event_seq IN ({seq_ph}) "
        "ORDER BY created_at ASC, id ASC"),
}

#: repo_key 的 IN 单批上限（与既有分块查询同值；正常入参 = 一个需求的候选仓库键，远小于此）。
_PIPELINE_STATUS_CHUNK = _ISSUE_ID_CHUNK


def _pipeline_status_stmt(name: str, repo_keys: list, extra: tuple = (),
                          seqs: list | None = None) -> tuple[str, tuple]:
    """展开 `{repo_ph}` / `{seq_ph}`，返回 (SQL, 参数元组)。

    参数顺序与模板占位符顺序一致：repo_keys → seqs → 末尾固定谓词值（extra）。
    """
    sql = _PIPELINE_STATUS_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown pipeline status query: {name}")
    sql = sql.replace("{repo_ph}", ", ".join(["?"] * len(repo_keys)))
    params: list = list(repo_keys)
    if seqs is not None:
        sql = sql.replace("{seq_ph}", ", ".join(["?"] * len(seqs)))
        params.extend(seqs)
    params.extend(extra)
    return sql, tuple(params)


def _pipeline_status_rows(name: str, repo_keys: list, extra: tuple = (),
                          seqs: list | None = None,
                          chunk: int = _PIPELINE_STATUS_CHUNK) -> list[tuple]:
    """按 repo_key 分块执行（seqs 仍在单条语句内），返回合并后的行元组列表。"""
    size = max(1, int(chunk or _PIPELINE_STATUS_CHUNK))
    out: list[tuple] = []
    for i in range(0, len(repo_keys), size):
        sql, args = _pipeline_status_stmt(name, repo_keys[i:i + size], extra, seqs)
        with _any_cursor() as c:
            c.execute(sql, args)
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def _created_at_asc_key(value: Any) -> tuple:
    """`ORDER BY created_at ASC` 的 Python 等价键（NULL 在前：sqlite 与 MySQL 的 ASC 都如此）。"""
    if value is None:
        return (0, "")
    return (1, value)


def _id_asc_key(value: Any) -> tuple:
    """`ORDER BY id ASC` 的 Python 等价键（整型按数值、文本按字面，避免混型比较报错）。"""
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    return (1, 0.0, str(value if value is not None else ""))


def issue_business_stage_status_any(repo_keys, issue_number) -> tuple[str, str]:
    """需求级 `(stage, status)`：issue 单表权威（列序同原语句，无排序 → 取第一行）。

    与原 `SELECT stage, status ... WHERE repo_key IN (...) AND issue_number=?` 同口径：
    不做 status/stage 过滤（软删、终态行照旧命中），NULL → 空串。空 repo_keys 零 SQL。
    候选键正常 ≤ 4 个（一个仓库的各平台别名），分块只为占位符数量上限。
    """
    keys = list(repo_keys or [])
    if not keys:
        return ("", "")
    for row in _pipeline_status_rows("pipeline_issue_stage_status", keys, (str(issue_number),)):
        return (row[0] or "", row[1] or "")
    return ("", "")


def issue_state_stage_any(repo_keys, issue_id) -> str:
    """issue_state 兜底阶段（`issue_id` 形如 `#5`，与原调用方传参逐字一致）。

    无行/NULL → 空串；空 repo_keys 零 SQL。
    """
    keys = list(repo_keys or [])
    if not keys:
        return ""
    for row in _pipeline_status_rows("pipeline_issue_state_stage", keys, (str(issue_id),)):
        return row[0] or ""
    return ""


def segment_task_rows_any(repo_keys, seqs=None) -> list[tuple]:
    """按 repo_key（可限定 event_seq）取 `(id, event_seq, status, result_text)`。

    顺序 = 原语句 `ORDER BY created_at ASC, id ASC`：分块后按同一键重排（单块时即语句
    自身的顺序），保证调用方「同一 seq 保留最后创建的一条」的覆盖语义跨块一致。
    `seqs` 的过滤口径与原实现逐字一致：给空值/全空列表 → 不加该谓词（照旧整仓取）。
    `created_at` 只用于排序，不进入返回列。
    """
    keys = list(repo_keys or [])
    if not keys:
        return []
    want = [s for s in (seqs or []) if s]
    name = "pipeline_segment_task_rows_by_seq" if want else "pipeline_segment_task_rows"
    rows = _pipeline_status_rows(name, keys, (), want or None)
    rows.sort(key=lambda r: (_created_at_asc_key(r[4]), _id_asc_key(r[0])))
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def apply_issue_vcs_existence_any(deleted_ids: list, restored_ids: list,
                                  stamped_ids: list) -> int:
    """原子回写 issue 台账的 VCS 存在性三态（标删/恢复/盖时间戳），返回影响行数。

    原实现把三组 UPDATE 放在一次 `db_lock` + 一次 `db.commit()` 里；这里用 `_any_tx()`
    保持同一原子性（自有 DSN 真事务 / 单库回落进程库并提交），失败整批回滚后由调用方
    映射为 unavailable。幂等：同一批 id 重跑只会重复盖同一时间戳，行数不叠加业务语义。
    """
    total = 0
    with _any_tx() as c:
        for name, ids in (("issue_vcs_mark_deleted", deleted_ids),
                          ("issue_vcs_mark_restored", restored_ids),
                          ("issue_vcs_stamp_checked", stamped_ids)):
            for start in range(0, len(ids), _ISSUE_VCS_ID_CHUNK):
                part = list(ids[start:start + _ISSUE_VCS_ID_CHUNK])
                if not part:
                    continue
                sql, params = _issue_vcs_in_stmt(name, part)
                cur = c.execute(sql, params)
                total += int(getattr(cur, "rowcount", 0) or 0)
    return total


# ── A2A 派发门禁读路径（api/a2a_dispatch_guard.py）──────────────────────────
#
# events / event_skipped 归本域。原实现从 api 进程主库直发，三库拆分后 api 进程库
# 没有这两张表（视图 0 → 1146），四条门禁查询全被内部 except 吞成空结果：
#   1) 下游 running 判定恒「无下游」→ 上游重复派发（#1770 防抖退回原状，白烧执行槽位）；
#   2) 时间窗去重恒「无重复」→ 未带 idempotency_key 的派发可双写（唯一索引只兜带键路径）；
#   3) 活跃重复判定恒「无活跃」→ 同 issue+scene+agent 并发落第二个事件；
#   4) storm 观测恒 count=1（只剩 upcoming）→ 「反复跳过」永不告警。
#
# 语句文本固定在本模块；`statuses` 只作为**值**绑定，并按白名单过滤（表名/列名
# 不入参，调用方不能传 SQL）。活跃状态集合的默认值与调用方 `_ACTIVE_STATUS` 一致。
_A2A_EVENT_STATUSES = frozenset({"queued", "dispatched", "running", "completed",
                                 "failed", "cancelled", "timeout"})

A2A_ACTIVE_STATUS = ("running", "dispatched")

# `{ph}` 是 IN 列表占位符标记（按白名单过滤后的状态个数生成 `?,?,...`）。
_A2A_EVENT_QUERIES: dict[str, str] = {
    # 下游 running 判定 + 排队阻塞集合：同 Issue 全部活跃事件（agent 侧过滤在调用方）
    "a2a_active_event_rows": (
        "SELECT seq, agent_type, status, created_at, scene_id FROM events "
        "WHERE repo_key=? AND issue_id=? AND event_type='a2a_event' "
        "AND status IN ({ph}) ORDER BY seq DESC LIMIT ?"),
    # 活跃重复判定：同 Issue + 同目标 agent 的活跃事件（含 payload 供上下文比对）
    "a2a_active_dup_rows": (
        "SELECT seq, created_at, status, scene_id, payload FROM events "
        "WHERE repo_key=? AND issue_id=? AND event_type='a2a_event' "
        "AND agent_type=? AND status IN ({ph}) ORDER BY seq DESC LIMIT ?"),
}


def _a2a_event_stmt(name: str, statuses) -> tuple[str, list[str]]:
    """展开 `{ph}`：statuses 过白名单 + 去重保序，返回 (SQL, 状态值列表)。

    白名单外的值直接丢弃（原 `IN (...)` 对未知状态本就零命中，口径不变）；
    全被过滤时调用方不发起查询（等价空结果集）。
    """
    sql = _A2A_EVENT_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown a2a event query: {name}")
    wanted: list[str] = []
    for raw in (statuses or ()):
        val = str(raw or "").strip()
        if val in _A2A_EVENT_STATUSES and val not in wanted:
            wanted.append(val)
    return sql.replace("{ph}", ",".join(["?"] * len(wanted))), wanted


def a2a_active_event_rows(repo_key: str, issue_id: str,
                          statuses=A2A_ACTIVE_STATUS,
                          limit: int = 50) -> list[tuple]:
    """同 Issue 的 a2a_event 按状态集合取 `(seq, agent_type, status, created_at, scene_id)`。

    无状态可查（statuses 全不在白名单）返回 []，不发起查询。
    """
    sql, wanted = _a2a_event_stmt("a2a_active_event_rows", statuses)
    if not wanted:
        return []
    with _any_cursor() as c:
        c.execute(sql, (repo_key, issue_id, *wanted, max(1, int(limit or 50))))
        return [tuple(r) for r in (c.fetchall() or [])]


def a2a_active_dup_rows(repo_key: str, issue_id: str, agent_type: str,
                        statuses=A2A_ACTIVE_STATUS,
                        limit: int = 20) -> list[tuple]:
    """同 Issue + 同目标 agent 的活跃 a2a_event 取
    `(seq, created_at, status, scene_id, payload)`（最近 limit 条）。"""
    sql, wanted = _a2a_event_stmt("a2a_active_dup_rows", statuses)
    if not wanted:
        return []
    with _any_cursor() as c:
        c.execute(sql, (repo_key, issue_id, agent_type, *wanted,
                        max(1, int(limit or 20))))
        return [tuple(r) for r in (c.fetchall() or [])]


def a2a_window_dedup_rows(repo_key: str, issue_id: str, agent_type: str) -> list[tuple]:
    """时间窗去重候选：同 Issue + 同目标 agent 最近 20 条
    `(seq, created_at, status, scene_id)`（状态/时间窗判定留在调用方）。"""
    return [tuple(r) for r in read_all_any(
        "a2a_window_dedup_rows", (repo_key, issue_id, agent_type))]


def a2a_storm_event_times(repo_key: str, issue_id: str, agent_type: str) -> list[tuple]:
    """storm 观测：同 Issue + 同目标 agent 最近 50 条 a2a_event 的 `(created_at,)`。"""
    return [tuple(r) for r in read_all_any(
        "a2a_storm_event_times", (repo_key, issue_id, agent_type))]


def a2a_storm_skip_rows(repo_key: str, issue_id: str) -> list[tuple]:
    """storm 观测：同 Issue 最近 100 条 event_skipped 的 `(created_at, detail)`。"""
    return [tuple(r) for r in read_all_any(
        "a2a_storm_skip_rows", (repo_key, issue_id))]


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


def skill_display_name_by_id_any(skill_id) -> str | None:
    """按技能 id 取 active 技能的 display_name（任意拓扑）。

    返回 None 表示**无 active 行**（回退链继续往下走）；返回 "" 表示有行但
    display_name 为空（调用方回退 skill_name）。两者语义不同，不能都压成空串，
    否则失活/不存在的 id 会让回退链提前终止在 skill_name 上。
    """
    row = read_one_any("skill_display_name_by_id", (int(skill_id),))
    return None if row is None else str(row[0] or "")


def skill_display_name_by_scope_any(skill_name: str, scope: str, owner_id: str) -> str | None:
    """按 (skill_name, scope, owner_id) 取 active 技能的 display_name（任意拓扑）。

    None/"" 的区分同 `skill_display_name_by_id_any`。原实现把 user / repo 两档写成
    两条字面量 SQL（`scope='user'` / `scope='repo'`），档位只是谓词值，合并成一条
    参数化语句后由调用方传 `scope`。
    """
    row = read_one_any("skill_display_name_by_scope", (skill_name, scope, owner_id))
    return None if row is None else str(row[0] or "")


def skill_active_display_rows_any() -> list:
    """全部 active 技能的 (skill_name, display_name, scope, owner_id) 行（任意拓扑）。

    批量显示名映射用；过滤只有 `is_active=1`，不带 scope 条件 —— 档位优先级由
    调用方在 Python 侧判定（他人 user 档 / 他仓 repo 档不写映射），与原批量 SQL
    逐字一致。
    """
    return read_all_any("skill_active_display_rows")


def skill_agent_type_by_priority_any(skill_name: str, repo_key: str = "") -> str:
    """按技能名取「本仓 repo → official → 其它仓 repo → 其它」优先级首行的 agent_type。

    过滤与排序口径照抄原语句：`skill_name` + `is_active=1`，`repo_key` 作
    `owner_id` 参与 CASE 优先级；空仓键不特判（那一支永不命中，与原实现一致）。
    无行 / 空技能名返回空串（原实现同样回退空串，不做异常上抛）。
    """
    if not skill_name:
        return ""
    row = read_one_any("skill_agent_type_by_priority", (skill_name, repo_key or ""))
    return str(row[0] or "") if row else ""


def skill_rebind_candidate_rows_any(skill_name: str, repo_key: str,
                                    agent_type: str = "", limit: int = 5) -> list[tuple]:
    """可改绑去向行 `(id, skill_name, scope, agent_type)`：本仓 repo 优先于 official。

    `agent_type` 为空走不带类型谓词的登记语句，非空走带 `agent_type=?` 的版本；
    两条具名查询把 SQL 完整留在本域，调用方不接收也不拼接 where 片段。`limit` 参数化绑定
    （原实现即 `LIMIT ?`）。空技能名直接返回空列表（原实现的早退分支）。
    """
    if not skill_name:
        return []
    if agent_type:
        rows = read_all_any("skill_rebind_candidates_typed",
                            (skill_name, repo_key, agent_type, int(limit)))
    else:
        rows = read_all_any("skill_rebind_candidates", (skill_name, repo_key, int(limit)))
    return [tuple(r) for r in rows]


def get_meta_value_any(key: str) -> str:
    """读 meta 配置值（任意拓扑；不存在返回空串）。"""
    row = read_one_any("meta_value", (key,))
    return (row[0] or "") if row else ""


def set_meta_value_any(key: str, value: str) -> None:
    """写 meta 配置值（任意拓扑）：**单语句**幂等 upsert。

    存在性只由主键回答，不看 UPDATE 的 rowcount（MySQL 计「实际改变的行」，
    同值重写返回 0；SQLite 计「匹配行」——同一份两段式实现在 SQLite 上全绿、
    在 MySQL 上必炸，2026-11-21 每次启动一条 1062 实测为证）。
    """
    with _any_cursor(write=True) as c:
        c.execute(_any_sql("meta_upsert"), (key, value))


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
                        "is_featured", "like_count", "category_id",
                        "cat_l1_id", "cat_l2_id", "tags", "summary")


def _featured_skill_row(r) -> dict[str, Any]:
    return {"id": r[0], "skill_name": r[1], "display_name": r[2] or r[1],
            "description": r[3] or "", "agent_type": r[4] or "",
            "is_featured": r[5], "like_count": r[6], "category_id": r[7] or 0,
            "cat_l1_id": (r[8] if len(r) > 8 else 0) or 0,
            "cat_l2_id": (r[9] if len(r) > 9 else 0) or 0,
            "tags": (r[10] if len(r) > 10 else "") or "",
            "summary": (r[11] if len(r) > 11 else "") or ""}


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


def task_attribution_row_any(task_id: str):
    """任务归属五元组 `(repo_key, agent_type, runtime_id, user_id, issue_id)`；无行 None。

    任意拓扑（自有 DSN → db_registry，单库/测试 → 进程库）。列序与原调用点兜底 SQL
    逐字一致，调用方按下标解包；`scene_id` 之类的派生口径留在调用方，不下沉到本域。
    """
    tid = str(task_id or "")
    if not tid:
        return None
    row = read_one_any("task_attribution_by_id", (tid,))
    if isinstance(row, dict):
        return (row.get("repo_key"), row.get("agent_type"), row.get("runtime_id"),
                row.get("user_id"), row.get("issue_id"))
    return row


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


# ── 能效看板取数原语（insight/observability/efficiency 的唯一取数口）──────
#
# issue / agent_tasks 归本域。原实现在 insight 的能效看板模块里用调用方游标直发
# 这三条 SQL：三库拓扑下 insight 进程库没有这两张表（视图 0）→ 1146，看板十张卡
# 里 issue 侧（②③①⑥⑤⑦⑧⑨⑩ 基础数据）全空、⑨⑩ 只剩「未分配」。
# 列序与原语句逐字一致（调用方按下标解包）；分块 500/批（原 issue 语句一次带走
# 全部 repo_key，仓库候选通常 1~3 个，分块只为 SQLite 变量上限，行集合不变）。

_EFFICIENCY_CHUNK = 500


def efficiency_issue_rows(repo_keys, cursor: Any = None) -> list[tuple]:
    """看板 issue 轻量行 (status, created_at, completed_at, created_by, assignee, user_id)。

    子任务行排除（`is_subtask_mode=0`，NULL 同样不计 —— SQL 三值逻辑原样保留）。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    out: list[tuple] = []
    with _owner_cursor(cursor) as c:
        for i in range(0, len(keys), _EFFICIENCY_CHUNK):
            part = keys[i:i + _EFFICIENCY_CHUNK]
            ph = ",".join(["?"] * len(part))
            c.execute(
                "SELECT status, created_at, completed_at, created_by, assignee, user_id"
                f" FROM issue WHERE repo_key IN ({ph}) AND is_subtask_mode=0", tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def efficiency_issue_detail_rows(repo_keys, cursor: Any = None) -> list[tuple]:
    """看板下钻 issue 详情行（10 列，列序与原查询逐字一致，含 COALESCE 默认值）。"""
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    out: list[tuple] = []
    with _owner_cursor(cursor) as c:
        for i in range(0, len(keys), _EFFICIENCY_CHUNK):
            part = keys[i:i + _EFFICIENCY_CHUNK]
            ph = ",".join(["?"] * len(part))
            c.execute(
                "SELECT issue_number, title, status, COALESCE(stage,''), COALESCE(scene_id,''),"
                " created_at, completed_at, COALESCE(created_by,''), COALESCE(assignee,''),"
                " COALESCE(user_id,0)"
                f" FROM issue WHERE repo_key IN ({ph}) AND is_subtask_mode=0", tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def efficiency_task_rows(repo_keys, cursor: Any = None) -> list[tuple]:
    """看板任务交付行 (status, started_at, completed_at, agent_type)。

    时间列原是带时区 UTC ISO，减法的时区抵消口径留在调用方；这里只负责取数。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if not keys:
        return []
    out: list[tuple] = []
    with _owner_cursor(cursor) as c:
        for i in range(0, len(keys), _EFFICIENCY_CHUNK):
            part = keys[i:i + _EFFICIENCY_CHUNK]
            ph = ",".join(["?"] * len(part))
            c.execute(
                "SELECT status, started_at, completed_at, agent_type"
                f" FROM agent_tasks WHERE repo_key IN ({ph})", tuple(part))
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


def strip_task_diagnostics(errors_raw) -> str:
    """去掉 agent_tasks.errors 里 diagnostic=True 的条目（#2790 token 诊断等）。

    `errors` 既是诊断通道又是失败原因来源：诊断条目不得抢占「真实原因」。
    实现唯一权威在 runtime 域（`service._strip_task_diagnostics`），此处只做
    **跨域公共原语**转发——insight（trace）/ routes（事件详情）不再各写一份口径。
    解析失败 / 非 JSON / 非 list → 原样返回（历史兼容）。
    """
    from src.domains.runtime.engine.service import _strip_task_diagnostics
    return _strip_task_diagnostics(errors_raw)


def summarize_task_errors(errors_raw, limit: int = 5) -> list[str]:
    """任务失败归因摘要：`agent_tasks.errors` → 展示用字符串列表（最多 limit 条）。

    事件详情 `task_errors`（#494）用：诊断条目先按统一口径滤掉，再逐条 str 截断 500；
    非 JSON 纯文本原样透传，空值 → 空列表。
    """
    _kept = strip_task_diagnostics(errors_raw)
    if not _kept:
        return []
    try:
        _parsed = json.loads(str(_kept))
    except (ValueError, TypeError):
        return [str(_kept)[:500]]
    if isinstance(_parsed, list):
        _out = [str(e)[:500] for e in _parsed if str(e).strip()]
    else:
        _out = [str(_kept)[:500]]
    return _out[:max(0, int(limit))]


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
                         subscribe_events: str = "[]", engine_uid: str = "",
                         uid_written: list | None = None) -> int:
    """建 repo 级 agent 会话行（id 由调用方生成，保证主键稳定）。返回 1=新建 / 0=已存在。

    uid_written: 可选出参——[bool] 收到 engine_uid 是否实际写入（旧库缺列降级时 False）。

    幂等由 SQL 侧的 OR IGNORE 保证。返回值必须**读游标** rowcount：exec_any 在自有 DSN 路径上
    返回的是 pymysql execute() 的 int（恒 0），拿它当「新建/已存在」判据永远得到 0。
    此前这里返回 None，而同名旧定义（本文件顶部，已删除）返回 rowcount —— 两个定义共存时
    后者覆盖前者，调用方拿到的返回值语义完全取决于定义顺序。
    """
    with _any_cursor(write=True) as c:
        # Issue #868 (T10)：engine_uid 双写轨道；旧库缺列（迁移未跑）降级 legacy INSERT
        _uid_ready = False
        try:
            _uid_ready = "engine_uid" in {r[1] for r in c.execute(
                "PRAGMA table_info(devops_agent_session)").fetchall()}
        except Exception:
            _uid_ready = False
        if _uid_ready:
            c.execute(_ANY_QUERIES["devops_agent_session_insert_uid"],
                      (session_id, repo_key, agent_type, runtime_id, engine_uid or "",
                       skill, skill_id, repo_gitcode_token or "", status, enable, subscribe_events))
            if uid_written is not None:
                uid_written.append(True)
        else:
            if uid_written is not None:
                uid_written.append(False)
            c.execute(_ANY_QUERIES["devops_agent_session_insert"],
                      (session_id, repo_key, agent_type, runtime_id, skill, skill_id,
                       repo_gitcode_token or "", status, enable, subscribe_events))
        return int(getattr(c, "rowcount", 0) or 0)

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


def reconcile_engine_references(runtime_ids: list, keep_id: int = 0, reason: str = "") -> dict:
    """Issue #3097：引擎行引用对账的跨域写原语（供 mgmt 等**非 runtime 进程**调用）。

    为什么需要这个原语：引用对账碰的 6 张表（agent_runtimes / agent_tasks /
    agent_type_runtime_pool / engine_capabilities / devops_agent_session /
    chat_agent_session）按 `configs/table_ownership.json` 全归 runtime 库。
    其它进程若拿自己的游标执行这些 SQL，三库拓扑下每条都是 1146 —— 因此跨进程调用
    必须落到 **runtime 域连接**上：`_any_tx()` 在自有 DSN 时开 runtime 真事务连接
    （独立提交，调用方事务不含这些表的写），单库/复用进程库时回落进程库。

    判定逻辑在 `engine.refs_reconcile.RuntimeRefReconciler`；本函数只做通道接线。
    """
    from src.domains.runtime.engine.refs_reconcile import reconcile_runtime_references

    with _any_tx() as c:
        return reconcile_runtime_references(c, list(runtime_ids or []), keep_id, reason=reason)


def upsert_chat_config_row(user_id: int, runtime_id: int = 0, skill: str = "",
                           skill_id: int = 0, engine_uid: str = "") -> None:
    """写用户全局 Chat 配置行（`sess-cfg-<uid>`）：先收敛同用户其它配置行，再 upsert。

    Issue #868 (T10)：engine_uid 双写（runtime_id 的 uid 镜像）；旧库缺列降级 legacy upsert。
    """
    sid = f"sess-cfg-{user_id}"
    with _any_tx() as c:
        c.execute(_any_sql("chat_config_delete_others"), (user_id, sid))
        _uid_ready = False
        try:
            _uid_ready = "engine_uid" in {r[1] for r in c.execute(
                "PRAGMA table_info(chat_agent_session)").fetchall()}
        except Exception:
            _uid_ready = False
        if _uid_ready:
            c.execute(_any_sql("chat_config_upsert_uid"),
                      (sid, user_id, "", runtime_id, engine_uid or "", skill, skill_id))
        else:
            c.execute(_any_sql("chat_config_upsert"), (sid, user_id, "", runtime_id, skill, skill_id))


def join_feishu_chat(chat_id: str, conv_id: str, user_id) -> bool:
    """飞书接入指针原子切换：校验属主 → 清旧持有者 → 设新（同一事务）。

    与原 kernel 实现等价：目标会话不存在或不属于该用户时返回 False，且不做任何写。
    """
    with _any_tx() as c:
        c.execute(_any_sql("conversation_owner"), (conv_id,))
        row = c.fetchone()
        # IM 侧 uid 来自渠道安装/身份表（TEXT：uuid 或 "1"，见 installation_owner_by_app_any 的
        # str 保形），而 chat_conversations.user_id 在 sqlite 是 INTEGER。Python 层比较不做类型
        # 亲和，"1" != 1 会恒定 deny（表现：/chat 选号回「❌ 加入失败」）。统一按字符串比对。
        if not row or str(row[0]) != str(user_id):
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
    # Issue #868 (T10)：engine_uid 双读列（runtime_id 的 uid 引用）；新库走迁移 202611030005
    ("engine_uid", "VARCHAR(36) DEFAULT NULL"),
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


# ---------- clone 端点契约（clone_endpoint 单点派生的对外出口）----------
# mgmt 等其它域要拼 clone 地址/用户名时只能走这里，禁止 import runtime.vcs.* 内部模块。
try:  # 异常类一并导出，调用方按它做错误分类
    from src.domains.runtime.vcs.clone_endpoint import CloneEndpointError  # noqa: F401
except Exception:  # pragma: no cover - 极端导入失败时给出可捕获的替身
    class CloneEndpointError(RuntimeError):  # type: ignore[no-redef]
        pass


def clone_params(repo_key: str) -> dict:
    """按平台卡派生 clone 参数（provider/owner/repo/clone_url/clone_auth/username）。"""
    from src.domains.runtime.vcs.clone_endpoint import clone_params as _impl

    return _impl(repo_key)


def clone_url(repo_key: str) -> str:
    """仓库 clone 地址（平台卡派生的唯一实现）。"""
    from src.domains.runtime.vcs.clone_endpoint import clone_url as _impl

    return _impl(repo_key)


def username_for_token(provider: str, token: str, repo_key: str = "") -> tuple:
    """令牌所属账号名（basic_password 平台用；返回 (username, source)）。"""
    from src.domains.runtime.vcs.clone_identity import username_for_token as _impl

    return _impl(provider, token, repo_key)


def invalidate_clone_identity(provider: str, token: str = "") -> None:
    """清掉令牌→login 解析缓存（令牌轮换后调用；契约出口，别域不得直连内部模块）。"""
    from src.domains.runtime.vcs.clone_identity import invalidate as _impl

    _impl(provider, token)


def clone_auth_header(repo_key: str, token: str) -> str:
    """git 传输用的 Authorization 头值（Basic base64(user:token)，按卡的 clone_auth 取用户名）。

    为什么提供在契约里：mgmt 拼克隆命令时不能自己再实现一套 scheme/用户名派生
    （那就是第二个真源），也不能把 token 拼进 URL（进程列表/日志泄漏面）。
    """
    from src.domains.runtime.vcs.clone_endpoint import clone_params as _cp

    p = _cp(repo_key)
    scheme = str(p.get("clone_auth") or "x_access_token")
    user = str(p.get("username") or "")
    if not user:
        user = "x-access-token" if scheme == "x_access_token" else (
            "oauth2" if scheme == "oauth2" else str(p.get("owner") or ""))
    import base64 as _b64

    return "Basic " + _b64.b64encode(f"{user}:{token}".encode()).decode()


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
        # 兼容游标 execute 返回 rowcount(int)，必须先 execute 再在游标上 fetchone
        c.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = c.fetchone()
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


def events_search_global(keyword: str) -> list[tuple]:
    """全局事件搜索候选行（5 列：seq, repo_key, event_type, title, status）。

    `keyword` 命中 title / event_type / repo_key（两侧 LOWER，`%…%` 在本域拼，
    调用方不构造 SQL 片段）；seq 倒序取**前 10 行**。不按仓库过滤 —— 原调用点
    在 Python 里再筛「用户可见仓库」，且 LIMIT 先于筛选生效，所以权限判定留在
    调用方（口径与原实现一致，不把 LIMIT 语义改宽）。
    """
    kw = f"%{str(keyword or '').lower()}%"
    return [tuple(r) for r in read_all_any("events_search_global", (kw, kw, kw))]


def agent_types_search_global(keyword: str) -> list[tuple]:
    """全局智能体类型搜索（3 列：agent_type, display_name, icon）。

    `keyword` 命中 agent_type / display_name（两侧 LOWER，`%…%` 在本域拼）。
    与原 SQL 同口径：不过滤 `enable`、不排序、不限行。
    """
    kw = f"%{str(keyword or '').lower()}%"
    return [tuple(r) for r in read_all_any("agent_type_search_global", (kw, kw))]


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
    "is_featured, like_count, sub_scene, category_id, cat_l1_id, cat_l2_id, tags, summary")


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
    # 组织隔离：official 对全组织可见；repo/user 的可见性靠 owner，不追加 org_id。
    # 哨兵值：'' 与 '0' 都表示「不归属任何组织」（官方技能存的是 '0'，旧数据还有 NULL），
    # 三者都必须视为全局可见 —— 只认 '' 会把 73 条官方技能对已归属组织的用户全部过滤掉
    # （列表接口返回 total=0，前端显示「无技能」）。
    if org_id and scope not in ("repo", "user"):
        conds.append("(COALESCE(org_id,'') IN ('', '0') OR org_id=?)")
        params.append(org_id)
    return conds, params


def skill_search(scope=None, user_id="", repo_key="", org_id="", q: str = "",
                 agent_type: str = "", category_id: int = 0, sort: str = "",
                 page: int = 1, page_size: int = 0, cat_l1_id: int = 0,
                 cat_l2_id: int = 0) -> tuple[int, list[tuple]]:
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
    if cat_l1_id and int(cat_l1_id) > 0:
        conds.append("cat_l1_id=?")
        params.append(int(cat_l1_id))
    if cat_l2_id and int(cat_l2_id) > 0:
        conds.append("cat_l2_id=?")
        params.append(int(cat_l2_id))

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


# 可见性查询的仓库 IN 分块上限：guest 组织可能命中上千个 repo_key，
# 单条 IN 会在 MySQL 上撑大解析与包体；分块后按 id 去重，结果集不变。
_CRED_VIS_CHUNK = 200


def _cred_visibility_where(org_id: str = "", personal_org: bool = True,
                           uname: str = "", repo_keys=None) -> tuple[str, list]:
    """凭据可见性 WHERE 片段（三口径，`?` 占位符）。

    - 非个人组织：只按 org_id 隔离；
    - 指定仓库：global + 本人 user + 该仓 worker；
    - 其余：global + 本人 user。

    标识符（列名/表名）全部是本函数内的字面量，调用方只能传口径参数，
    不能传 SQL 片段。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    if org_id and not personal_org:
        return " AND org_id=?", [org_id]
    if keys:
        ph = ",".join(["?"] * len(keys))
        return (f" AND (scope='global' OR (scope='user' AND owner_name=?) OR repo_key IN ({ph}))",
                [uname, *keys])
    return " AND (scope='global' OR (scope='user' AND owner_name=?))", [uname]


def credential_ids_visible(org_id: str = "", personal_org: bool = True,
                           uname: str = "", repo_keys=None) -> list[int]:
    """可见凭据 id（三口径：非个人组织按 org_id / 指定仓库 / 个人全局）。

    与 dashboard 的 worker 可见性同一套语义，条件在本域拼；调用方只传口径参数。
    """
    frag, params = _cred_visibility_where(org_id, personal_org, uname, repo_keys)
    with _any_cursor() as c:
        c.execute("SELECT id FROM runtime_credentials WHERE status != 'revoked'" + frag, params)
        return [int(r[0]) for r in (c.fetchall() or [])]


def runtime_credentials_visible_rows(org_id: str = "", personal_org: bool = True,
                                     uname: str = "", repo_keys=None) -> list[tuple]:
    """可见凭据 `(id, device_name, name)`（未注销，按 id 升序）。

    insight 的设备资源曲线原先把可见性 WHERE 片段拼进 SQL 直查本表：三库拆分后
    进程库没有 runtime_credentials（视图 0）→ 1146 被 except 吞成空列表，曲线恒空。
    谓词三口径与 `credential_ids_visible` 同源（同一 helper），本函数只多取两列。

    仓库 IN 列表按 `_CRED_VIS_CHUNK` 分块：`scope='global'` 行会命中每个分块，
    因此跨块按 id 去重后仍按 id 升序返回（结果集与顺序和原单条 SQL 一致）。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    rows: dict[int, tuple] = {}
    if org_id and not personal_org:
        chunks: list[list[str]] = [keys]
    else:
        chunks = [keys[i:i + _CRED_VIS_CHUNK] for i in range(0, len(keys), _CRED_VIS_CHUNK)]
    for chunk in chunks or [[]]:
        frag, params = _cred_visibility_where(org_id, personal_org, uname, chunk)
        with _any_cursor() as c:
            c.execute("SELECT id, device_name, name FROM runtime_credentials"
                      " WHERE status != 'revoked'" + frag, params)
            for r in (c.fetchall() or []):
                rows[int(r[0])] = tuple(r)
    return [rows[k] for k in sorted(rows)]


# ── 总览聚合基集（insight/observability/overview.py）──
# 原实现两条 SQL 用 insight 进程主库游标直查本域两张表（agent_tasks / runtime_credentials）。
# 三库独立后 insight 进程库没有这两张表（视图 0）→ 1146，被调用方同名 `except` 吞成
# 「任务图全空 / 成功率 None / 设备 0/0」，日志不带表名，页面静默降级。
# 本原语把两条语句收在本域：自有 DSN 打 runtime，单库/测试拓扑回落进程库，两条拓扑
# 同一份语句。语句文本固定在本模块（`{ph}` 只按值个数展开占位符）；调用方只能传值
# 列表与可见性口径参数，不接受拼接 SQL / 表名 / 列名。

_OBS_OVERVIEW_TASK_QUERY = (
    "SELECT id, status, repo_key, agent_type, created_at, started_at, completed_at"
    " FROM agent_tasks WHERE repo_key IN ({ph})")

# 凭据基集：`id` 仅作跨分块去重键（`scope='global'` 行会命中每个分块），
# 返回给调用方的仍是原 SQL 的两列 `(status, last_seen_at)`，列序不变。
_OBS_OVERVIEW_CREDENTIAL_STATUS_QUERY = (
    "SELECT status, last_seen_at, id FROM runtime_credentials WHERE status != 'revoked'")


def obs_overview_task_rows_any(repo_keys) -> list[tuple]:
    """总览任务基集（7 列，列序与 insight 原 SQL 逐字一致；无排序、无窗口过滤）。

    列为 `(id, status, repo_key, agent_type, created_at, started_at, completed_at)`。
    时间窗与分桶在调用方（时间格式跨方言，见 overview 模块头注释 1）；空/None 仓库键
    与重复键丢弃（`IN` 本身对重复值只回一行，语义等价），空列表返回 `[]` 且零 SQL。
    仓库键按 `_CRED_VIS_CHUNK` 分块：agent_tasks 每行只有一个 repo_key，分块之间不会
    重复，行集合与一次 `IN` 等价。
    """
    keys: list[str] = []
    seen: set[str] = set()
    for k in (repo_keys or []):
        s = str(k or "")
        if not s or s in seen:
            continue
        seen.add(s)
        keys.append(s)
    out: list[tuple] = []
    for i in range(0, len(keys), _CRED_VIS_CHUNK):
        part = keys[i:i + _CRED_VIS_CHUNK]
        ph = ", ".join(["?", ] * len(part))
        with _any_cursor() as c:
            c.execute(_OBS_OVERVIEW_TASK_QUERY.replace("{ph}", ph), tuple(part))
            out.extend(tuple(r) for r in (c.fetchall() or []))
    return out


def obs_overview_credential_status_rows_any(org_id: str = "", personal_org: bool = True,
                                            uname: str = "", repo_keys=None) -> list[tuple]:
    """可见凭据的 `(status, last_seen_at)`（未注销；两列列序同 insight 原 SQL）。

    三口径可见性与 `credential_ids_visible` / `runtime_credentials_visible_rows` 同源
    （同一个 `_cred_visibility_where`）：非个人组织按 `org_id` 隔离；指定仓库时
    `global + 本人 user + 该仓 worker`；否则 `global + 本人 user`。
    只回元组原值 —— 在线判定（`derive_runtime_health`）与「非 revoked 也算总数」的
    计数口径是 insight 展示语义，留调用方。跨分块按 id 去重后按 id 升序返回
    （结果集与一次 `IN` 等价；顺序确定，便于断言）。
    """
    keys = [str(k) for k in (repo_keys or []) if k]
    rows: dict[Any, tuple] = {}
    if org_id and not personal_org:
        chunks: list[list[str]] = [[]]     # org 隔离口径不看仓库键
    else:
        chunks = [keys[i:i + _CRED_VIS_CHUNK] for i in range(0, len(keys), _CRED_VIS_CHUNK)]
    for chunk in chunks or [[]]:
        frag, params = _cred_visibility_where(org_id, personal_org, uname, chunk)
        with _any_cursor() as c:
            c.execute(_OBS_OVERVIEW_CREDENTIAL_STATUS_QUERY + frag, params)
            for r in (c.fetchall() or []):
                rows[r[2]] = (r[0], r[1])
    return [rows[k] for k in sorted(rows)]


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
                       page: int = 1, page_size: int = 50, agent_type: str = "") -> tuple:
    """场景页技能下拉：可见范围 = 官方 + 本人 + 该仓，可按关键词/范围/角色过滤，返回 (total, rows)。

    条件在本域拼（skill 归运行域），调用方只给口径参数。
    agent_type 非空时按职责过滤：只返回同角色技能与「未标注角色」的通用技能
    （存量内建包 agent_type 为空，一律视为通用，否则下拉会被清空）。
    """
    where = ["(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))"]
    params: list = [user_id, repo_key]
    if agent_type:
        where.append("(agent_type=? OR agent_type='' OR agent_type IS NULL)")
        params.append(agent_type)
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
# 归属：`configs/table_ownership.json` → grape_runtime_db.distributed_locks。
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


def lock_lease_status_any(lock_key: str) -> tuple | None:
    """租约状态（任意拓扑）：`(holder, expires_at)`，无锁返回 None。

    列序与原直发句逐字一致（`SELECT holder, expires_at FROM distributed_locks
    WHERE lock_key=?`），**不带** expiry 过滤——调用方（insight 采样租约的排查入口）
    需要看到「已过期但尚未被清理/接管」的行，这是同一个排查语义，不改口径。
    负参（KeyError）不吞：未登记查询属于编码错误。
    """
    return read_one_any("lock_row", (lock_key,))


# ── 指标桶取数（insight/observability/metric_rollup 跨域收口）─────────────────────
# 归属：`configs/table_ownership.json` → grape_runtime_db.{agent_tasks,events,
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
    # 兼容游标（db_registry._CompatCursor）的 execute 返回 rowcount(int)，不是游标本身：
    # 三库拓扑下域连接走的正是它，链式取 description 会炸（'int' has no attribute）。
    return column in [str(d[0]) for d in ((cur if hasattr(cur, "description") else c).description or [])]


def runtime_heartbeat_alive_any(runtime_id) -> bool:
    """引擎行是否「status=online 且心跳新鲜」——派发前校验 runtime_id 仍可定向。

    语义与改造前逐字一致：行不存在 / ``last_seen_at`` 空 / 时间戳不可解析 → False；
    ``now - last_seen_at <= RUNTIME_STALE_SECONDS`` 才算活（**恰好等于阈值仍算活**，
    未来时间戳因差值为负也判活）。**不看 credential_id**：原调用点对本机直连行同样
    要求 online+心跳，与 ``engine_health_ok`` 的分形态口径（cid=0 免心跳）不同，勿合并。

    为什么归本域：``agent_runtimes`` 是运行域表，``RUNTIME_STALE_SECONDS`` 与
    ``parse_dt`` 也都是本域口径；api 侧只该拿布尔。改造前 api 在**自己的**连接上直发
    这条 SQL，三库独立后必 1146（表不在 api 库），校验抛错把整条派发打成 error ——
    即「带 runtime_id 的节点在独立拓扑下整条派发失败」。任意拓扑：自有 DSN 走
    db_registry，单库/测试拓扑回落进程库。
    """
    rid = int(runtime_id or 0)
    if rid <= 0:
        return False
    try:
        row = read_one_any("runtime_last_seen_online", (rid,))
    except Exception as e:  # 表缺失/连接失败：按「不活」降级 = 原「落池任务」路径
        log.warning(f"runtime_heartbeat_alive_any: lookup failed id={rid}: {e}")
        return False
    if not row or not row[0]:
        return False
    from src.domains.runtime.engine.health import parse_dt
    from src.domains.runtime.engine.engine_select import _RUNTIME_STALE_SECONDS
    ts = parse_dt(row[0])
    if ts is None:
        return False
    return time.time() - ts <= _RUNTIME_STALE_SECONDS


def runtime_model_name_any(runtime_id) -> str:
    """引擎行上配置的模型名（`agent_runtimes.model_name`），取不到一律空串。

    任意拓扑：自有 DSN 走 db_registry，单库/测试拓扑回落进程库。探测与读取都在
    **本域连接**上做——原调用点（insight/observability/llm_usage）在 insight 连接上
    探测/直读运行域表，三库独立后恒 1146（被 except 吞成 warning，配置模型回落永久失效）。
    #865 老库无 `model_name` 列 → 静默跳过（与原「缺列不报错」同口径），其余错误上抛
    由调用方决定降级。
    """
    rid = int(runtime_id or 0)
    if rid <= 0:
        return ""
    with _any_cursor() as c:
        try:
            has_col = _table_has_column(c, "agent_runtimes", "model_name")
        except Exception as e:  # 表缺失（测试库/极老库）：配置回落跳过，不报错
            log.debug(f"agent_runtimes column probe skipped id={rid}: {e}")
            return ""
        if not has_col:
            log.debug(f"agent_runtimes.model_name absent; config fallback skipped id={rid}")
            return ""
        c.execute(_any_sql("agent_runtimes_model_name"), (rid,))
        row = c.fetchone()
    if not row:
        return ""
    val = row.get("model_name") if isinstance(row, dict) else row[0]
    return str(val or "").strip()


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


# ---------- 进程运维诊断取数（api server_common 的 /healthz 与 /health/realtime）----------
#
# 四条原 SQL 都在 api 进程主库直发：`SELECT COUNT(*) FROM agent_tasks`（readiness 表探活）、
# 状态分布、在制计数、`SELECT status, last_seen_at, credential_id FROM agent_runtimes`。
# 三库拆分后 api 进程库没有这两张表（视图 0）→ /healthz 恒 `not_ready`（tables 报
# agent_tasks missing）、/health/realtime 恒 `db_error`，运维面看不出真实在制/在线数。
# 表归 runtime，SQL 只留本域；四条语句列序/谓词与原实现逐字一致，任意拓扑可用。

def health_task_rows_total() -> int:
    """agent_tasks 行数（readiness 的表存在性探活；表缺失时抛异常，由调用方判 not_ready）。"""
    row = read_one_any("health_task_rows_total")
    return int((row or [0])[0] or 0)


def health_task_status_counts() -> list[tuple]:
    """任务状态全量分布 `[(status, count), ...]`（不按 repo/org 过滤，运维诊断口径）。"""
    return [tuple(r) for r in read_all_any("health_task_status_counts")]


def health_task_inflight_count() -> int:
    """在制任务数（`status IN ('dispatched','running')`）。"""
    row = read_one_any("health_task_inflight_count")
    return int((row or [0])[0] or 0)


def health_runtime_rows() -> list[tuple]:
    """全部引擎行 `(status, last_seen_at, credential_id)`；在线/离线判定归调用方。"""
    return [tuple(r) for r in read_all_any("health_runtime_rows")]


# ---------- 业务采样器（insight/observability/__init__.collect_business_sampler）----------
#
# 归属：`configs/table_ownership.json` → agent_tasks / agent_runtimes 均归本域。
# 原实现在 insight 侧持 insight 进程库游标直发 4 条语句（`with db_lock: c = db.cursor()`，
# 每条各自 `try/except: pass`）。三库拓扑下 insight 库没有这两张表（视图 0）→ 4 条必
# 1146 并被吞成「队列/执行/卡住全 0、在线 0」，/metrics 上的业务 gauge 静默归零：
# 面板显示的是「业务停摆」而不是「采不到数」。表归本域，语句只留本域一份。
#
# 口径逐字照抄原语句（`COALESCE(agent_type,'unknown')` + `COUNT(*)` + 同一 GROUP BY），
# 四条在**同一个** `_any_cursor()` 上顺序执行 —— 单库/测试拓扑下就是一次 `db_lock`
# 持有（原实现也是一把锁内跑完四条），自有 DSN 下是同一条连接，保住「一次采样 = 一个
# 快照」的语义。单条失败只影响该条（该键返回空），与原来「每条独立吞异常」等价，
# 但不留静默 except：降级为 log.debug，其余三条照常出数。


def _sampler_source_counts(rows) -> dict[str, int]:
    """行 `[(agent_type, n), ...]` → `{source: n}`；空值归 `"unknown"`（原实现同口径）。

    原实现是 `queued_by_source[r[0] or "unknown"] = r[1]`：`COALESCE` 只把 NULL 变
    `'unknown'`，空串靠 Python 的 `or` 兜住，这里保持同一条归一规则与同行覆盖语义。
    """
    out: dict[str, int] = {}
    for r in rows or []:
        out[r[0] or "unknown"] = int(r[1] or 0)
    return out


def task_sampler_snapshot_any(stuck_cutoff: str) -> dict[str, Any]:
    """业务采样器一次取数（任意拓扑）：按 source 的任务计数 + 引擎行。

    返回 `{"queued": {...}, "running": {...}, "stuck": {...},
    "runtimes": [(status, last_seen_at, credential_id), ...]}`。

    `stuck_cutoff` 是 ISO 时间串，谓词 `started_at < ?` 与原实现同义（严格早于才算
    卡住，等值不算）。`runtimes` 复用已登记的 `health_runtime_rows`（同一语句、同列序），
    不在本域把同一语句登记两份。单条查询失败 → 该键为空（`{}` / `[]`）并落 log.debug，
    四条互不影响，调用方不必再为降级包 try/except。
    """
    snapshot: dict[str, Any] = {"queued": {}, "running": {}, "stuck": {}, "runtimes": []}
    plan = (("queued", "obs_sampler_task_queued", ()),
            ("running", "obs_sampler_task_running", ()),
            ("stuck", "obs_sampler_task_stuck", (str(stuck_cutoff or ""),)))
    with _any_cursor() as c:
        for key, name, params in plan:
            try:
                # 分开写：pymysql 传空参数元组会把语句里的 % 当格式符（本仓既有坑）
                if params:
                    c.execute(_any_sql(name), params)
                else:
                    c.execute(_any_sql(name))
                snapshot[key] = _sampler_source_counts(c.fetchall())
            except Exception as e:  # noqa: BLE001 - 单条降级，不影响其余三条
                log.debug(f"obs sampler query {name} skipped: {e}")
        try:
            c.execute(_any_sql("health_runtime_rows"))
            snapshot["runtimes"] = [tuple(r) for r in (c.fetchall() or [])]
        except Exception as e:  # noqa: BLE001 - 引擎行缺失只影响在线数
            log.debug(f"obs sampler runtime rows skipped: {e}")
    return snapshot


# ---------- 可观测性「failed 空 result 占比」取数（insight/observability/failed_gap.py）----------
#
# 归属：`configs/table_ownership.json` → grape_runtime_db.events（本域）。
# 原实现在 insight 侧是双路径：`runtime_lib()` 可用（本域配了自有 DSN）→ 具名查询
# `count_events` / `query_events`；不可用 → insight 进程主库上直发 4 条 events SQL。
# 后者是单库兜底，也正是 insight 文件里跨域引用恒计 4 处的原因；而三库拓扑下
# insight 进程库没有 events（视图已退役 → 1146），4 条必失败并被外层 `except` 吞成
# 「全 0 + 空样本」——页面把「查不到」显示成「没有无头失败」，比报错更有害。
#
# 本组把两条路径合成**一条任意拓扑通道**（`_any_cursor`：自有 DSN 走 db_registry，
# 单库/测试拓扑回落进程主库），调用方不再需要 if/else，也不在 insight 里留 SQL。
# 口径逐字照抄原语句：
# - 只统计 `status='failed'`；repo_key 候选集由调用方给（可见仓库，已在 insight 侧算好）；
# - 真空 = `result IS NULL OR TRIM(result)=''`（TRIM 在 MySQL/SQLite 同义）；
# - legacy = `result LIKE '<prefix>%'`，前缀由调用方传，`%` 由本域拼（调用方不造 SQL）；
# - 样本列序 `seq, repo_key, event_type, agent_type, issue_id, result, created_at`、
#   `ORDER BY seq DESC LIMIT ?`，与原语句同序。
# `{ph}` 按 repo_key 个数展开；语句文本固定在本模块，不接受 SQL/表名/列名入参。
_FAILED_GAP_QUERIES: dict[str, str] = {
    "failed_gap_total": (
        "SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({ph})"),
    "failed_gap_empty": (
        "SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({ph}) "
        "AND (result IS NULL OR TRIM(result)='')"),
    "failed_gap_legacy": (
        "SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({ph}) "
        "AND result LIKE ?"),
    "failed_gap_samples": (
        "SELECT seq, repo_key, event_type, agent_type, issue_id, result, created_at "
        "FROM events WHERE status='failed' AND repo_key IN ({ph}) "
        "AND (result IS NULL OR TRIM(result)='' OR result LIKE ?) "
        "ORDER BY seq DESC LIMIT ?"),
}

#: repo_key 候选集单批上限（SQLite 变量上限 / 超长语句保护；行集合与一次 IN 等价）。
_FAILED_GAP_KEY_CHUNK = 400


def _failed_gap_sql(name: str, count: int) -> str:
    """把 `{ph}` 展开成 count 个占位符；未登记名抛 KeyError（不静默退回任意 SQL）。"""
    sql = _FAILED_GAP_QUERIES.get(name)
    if sql is None:
        raise KeyError(f"unknown failed-gap query: {name}")
    return sql.replace("{ph}", ",".join(["?"] * max(0, int(count))))


def _failed_gap_keys(repo_keys) -> list[str]:
    """候选仓库集规整：丢空值、转字符串、保序（重复值由调用方保证不出现）。"""
    return [str(k) for k in (repo_keys or []) if k]


def failed_event_gap_counts_any(repo_keys, legacy_prefix: str = "[legacy]") -> dict[str, int]:
    """failed 事件的三个标量计数：{total_failed, empty_result, legacy}（任意拓扑）。

    空候选集返回全 0（原调用方在 `not repos` 时早退；这里兜住，避免 `IN ()` 语法错）。
    多块时逐块累加 —— 各块 repo_key 集合互斥，和等于一次 IN。
    """
    out = {"total_failed": 0, "empty_result": 0, "legacy": 0}
    keys = _failed_gap_keys(repo_keys)
    if not keys:
        return out
    like = f"{legacy_prefix}%"
    for start in range(0, len(keys), _FAILED_GAP_KEY_CHUNK):
        part = keys[start:start + _FAILED_GAP_KEY_CHUNK]
        n = len(part)
        with _any_cursor() as c:
            c.execute(_failed_gap_sql("failed_gap_total", n), tuple(part))
            out["total_failed"] += int((c.fetchone() or [0])[0] or 0)
            c.execute(_failed_gap_sql("failed_gap_empty", n), tuple(part))
            out["empty_result"] += int((c.fetchone() or [0])[0] or 0)
            c.execute(_failed_gap_sql("failed_gap_legacy", n), tuple(part) + (like,))
            out["legacy"] += int((c.fetchone() or [0])[0] or 0)
    return out


def failed_event_gap_samples_any(repo_keys, legacy_prefix: str = "[legacy]",
                                 limit: int = 20) -> list[tuple]:
    """最近「真空 + legacy」failed 事件样本（任意拓扑），按 seq 倒序、至多 limit 条。

    列序同上（调用方按下标取值）。`limit<=0` 返回空（等价原语句 `LIMIT 0`；路由层
    已把入参夹到 [1,100]，这里不再提升，避免放大样本数）。
    多块时每块先各取 limit 条，Python 合并后按 seq 倒序截断 —— 与一次 IN 等价。
    """
    lim = int(limit or 0)
    keys = _failed_gap_keys(repo_keys)
    if not keys or lim <= 0:
        return []
    like = f"{legacy_prefix}%"
    chunks = [keys[i:i + _FAILED_GAP_KEY_CHUNK]
              for i in range(0, len(keys), _FAILED_GAP_KEY_CHUNK)]
    rows: list[tuple] = []
    for part in chunks:
        with _any_cursor() as c:
            c.execute(_failed_gap_sql("failed_gap_samples", len(part)),
                      tuple(part) + (like, lim))
            rows.extend(tuple(r) for r in (c.fetchall() or []))
    if len(chunks) > 1:
        rows.sort(key=lambda r: int(r[0] or 0), reverse=True)
        rows = rows[:lim]
    return rows


# ── 账号注销前置检查：某批仓库的在制任务数（src/domains/mgmt/routes/grape_auth.py）──
# 背景：注销端点要回答「这些仓库上还有没有跑着的 Agent 任务」，原实现拿 kernel 进程主库
# 句柄（`session_manager.db` + `db_lock`）直发一条 COUNT。`agent_tasks` 归本域，三库
# 独立后 mgmt 进程库里没有它（视图 0）→ 1146；原语句没有 try，异常直接冒到端点，
# 表现为「注销 500」——只有单库拓扑一直能跑，所以测试与彩排都是绿的。
#
# 口径逐字保留：**不加** `is_active` / `user_id` / 时间窗谓词，也不改状态集合
# （`queued`/`dispatched`/`running` 三态，成功/失败/取消的历史任务不算在制）。
# `{ph}` 只按 repo_key 个数展开成占位符；语句文本固定在本模块，调用方只能传值列表，
# 不接受拼接 SQL / 表名 / 列名。
_INFLIGHT_TASK_QUERIES: dict[str, str] = {
    "inflight_task_count_by_repos": (
        "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('queued','dispatched','running') "
        "AND repo_key IN ({ph})"),
}

#: repo_key 候选集单批上限（SQLite 变量上限 / 超长语句保护；行集合与一次 IN 等价）。
_INFLIGHT_TASK_KEY_CHUNK = 400


def _inflight_task_sql(count: int) -> str:
    """把 `{ph}` 展开成 count 个占位符；未登记名抛 KeyError（不静默退回任意 SQL）。"""
    sql = _INFLIGHT_TASK_QUERIES.get("inflight_task_count_by_repos")
    if sql is None:
        raise KeyError("unknown inflight task query")
    return sql.replace("{ph}", ",".join(["?"] * max(0, int(count))))


def _inflight_repo_keys(repo_keys) -> list[str]:
    """候选仓库集规整：丢空值、转字符串、保序去重（COUNT 口径与重复键无关）。"""
    out: list[str] = []
    seen: set[str] = set()
    for k in (repo_keys or []):
        if not k:
            continue
        s = str(k)
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def inflight_task_count_any(repo_keys, chunk: int = _INFLIGHT_TASK_KEY_CHUNK) -> int:
    """这批仓库上的在制任务数（`queued`/`dispatched`/`running` 三态之和，任意拓扑）。

    空候选集返回 0（原调用方在 `not repo_keys` 时早退；这里兜住，避免 `IN ()` 语法错）。
    多块时逐块累加 —— 各块 repo_key 集合互斥，和等于一次 IN。调用方只需传值列表。
    """
    keys = _inflight_repo_keys(repo_keys)
    if not keys:
        return 0
    size = max(1, int(chunk or _INFLIGHT_TASK_KEY_CHUNK))
    total = 0
    for i in range(0, len(keys), size):
        part = keys[i:i + size]
        with _any_cursor() as c:
            c.execute(_inflight_task_sql(len(part)), tuple(part))
            total += int((c.fetchone() or [0])[0] or 0)
    return total


def event_issue_id_by_seq(repo_key: str, seq):
    """按 (仓库, 序号) 取事件关联的 issue_id；无行返回 None。"""
    with _any_cursor() as c:
        c.execute("SELECT issue_id FROM events WHERE repo_key=? AND seq=? LIMIT 1",
                  (repo_key, seq))
        return c.fetchone()


def first_online_runtime():
    """当前在线且健康的引擎 (id, name)，opencode/codearts 优先；无则 None。"""
    with _any_cursor() as c:
        c.execute(
            "SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 "
            "ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END "
            "LIMIT 1")
        return c.fetchone()


def terminal_task_statuses() -> tuple[str, ...]:
    """任务终态词表（单源在 kernel.task_states，本域反向引用，见 Issue #2508 review）。"""
    from src.kernel.task_states import TERMINAL_TASK_STATUSES
    return tuple(TERMINAL_TASK_STATUSES)


def task_status_by_id(task_id: str) -> str:
    """任务当前状态；空串表示任务不存在或查询失败。

    kernel 的 token 判定需要「任务是否已终态」，但 agent_tasks 归本域，kernel
    不能直查（check-arch A11）：由 src/api 层通过本原语取值后传进去。
    """
    if not task_id:
        return ""
    try:
        with _any_cursor() as c:
            c.execute("SELECT status FROM agent_tasks WHERE id=? LIMIT 1", (task_id,))
            row = c.fetchone()
        return str(row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"task_status_by_id failed id={task_id}: {e}")
        return ""


# ── G-02（A11 裁定 2026-11-17）：grape_t_cloud_sandboxes 归 runtime 后，mgmt 吊销链的写点 ──
# mgmt 凭据吊销时把该凭据名下沙箱行标记 orphaned（记录保留：用户去平台手动清理的对账
# 依据）。写发生在 runtime 归属表上，mgmt 进程不得直连——经本原语在 runtime 域内执行。

def mark_cloud_sandboxes_orphaned_by_provider(provider_id) -> int:
    """吊销级联：该 Provider 名下沙箱行 state='orphaned'、provider_id 清零（防悬空 JOIN）。

    返回受影响行数（0 = 该 Provider 无沙箱记录，吊销照常）。
    """
    with _any_cursor(write=True) as c:
        c.execute(
            "UPDATE grape_t_cloud_sandboxes SET state='orphaned', provider_id=0 "
            "WHERE provider_id=?",
            (provider_id,))
        return int(c.rowcount or 0)



# G-02 补充形态：mgmt user/db.py 的 uid 换锚/回填链需要与调用方**同一事务**读写凭据表
# （rebase 报表读旧 uid → 改绑 → 同事务提交，跨连接会读到陈旧快照，#868 实测测试缝
# 即此形态）。F1（守卫复检 201339604）：SQL 文本与执行面均留在 owner 域——导出**可调用
# 原语**（进 lib 注册面，`check_lib_query_names.py` 只认非下划线 callable），调用方游标
# 作参数传入以保同事务语义与 fixture 连接注入缝；mgmt 不再执行 runtime 表 SQL。
SQL_CREDENTIAL_UID_BY_ID = "SELECT credential_uid FROM runtime_credentials WHERE id=?"
SQL_CREDENTIAL_SET_UID = "UPDATE runtime_credentials SET credential_uid=? WHERE id=?"
SQL_CREDENTIAL_ID_UID_PAIRS = "SELECT id, credential_uid FROM runtime_credentials"


def credential_uid_by_id(cursor: Any, credential_id: int) -> str:
    """F1/A11：runtime_credentials 单行 credential_uid 读原语（调用方游标，同事务）。"""
    row = cursor.execute(SQL_CREDENTIAL_UID_BY_ID, (credential_id,)).fetchone()
    return (row[0] or "") if row else ""


def set_credential_uid(cursor: Any, credential_id: int, uid: str) -> None:
    """F1/A11：runtime_credentials credential_uid 写原语（调用方游标，同事务提交）。"""
    cursor.execute(SQL_CREDENTIAL_SET_UID, (uid, credential_id))


def credential_id_uid_pairs(cursor: Any) -> list[tuple]:
    """F1/A11：全表 (id, credential_uid) 对读原语（调用方游标，回填报表用）。"""
    return cursor.execute(SQL_CREDENTIAL_ID_UID_PAIRS, ()).fetchall()


# ── 引擎 uid 轨道维护（2026-11-18 跨域 import 批次 · mgmt.user）──────────────
# 迁移背景：runtime/routes/runtime.py 此前
#   from src.domains.mgmt.user.db import ensure_credential_uid / rebase_engine_uid_references
# 属跨域 import 别域内部模块；而这两条链路碰的表（runtime_credentials /
# agent_runtimes / agent_type_runtime_pool / engine_capabilities / agent_tasks /
# task_messages / devops_agent_session / chat_agent_session）按
# `configs/table_ownership.json` **全归 runtime 域**。判定逻辑 + SQL 收到 owner 域
# 一份；mgmt.user.db 的同名函数退化为转发壳（保留既有 import 面与测试缝），
# 调用方照旧传自己的游标（runtime 进程内即 runtime 域连接，同事务语义不变）。
_ENGINE_UID_REF_TABLES = (
    # (表名, 计数键) —— 与 mgmt 侧历史口径逐项一致（Issue #868）
    ("agent_type_runtime_pool", "pool_rows"),
    ("engine_capabilities", "capability_rows"),
    ("agent_tasks", "task_rows"),
    ("task_messages", "task_message_rows"),
    ("devops_agent_session", "devops_sessions"),
    ("chat_agent_session", "chat_sessions"),
)


def ensure_credential_uid(credential_id: int, cursor: Any = None) -> str:
    """确保凭据行有 credential_uid（UUIDv4，一次性终身不变）；未迁移旧库返回 ''。

    cursor 传入即复用调用方事务（注册链路：credential_uid → engine_uid 派生同事务，
    跨连接会读到陈旧快照）；不传则由本域自有 DSN 开事务。
    """
    from src.kernel.engine_uid import new_credential_uid

    def _ensure(c) -> str:
        existing = credential_uid_by_id(c, credential_id)
        if existing:
            return existing
        uid = new_credential_uid()
        set_credential_uid(c, credential_id, uid)
        return uid

    try:
        if cursor is not None:
            return _ensure(cursor)
        with db_registry.cursor("runtime") as c:
            return _ensure(c)
    except Exception:  # noqa: BLE001  旧库缺列=降级空串（与历史行为一致）
        return ""


def rebase_engine_uid_references(cursor: Any, old_uid: str, new_uid: str) -> dict:
    """单个 engine_uid 变更的引用轨道整体改绑（old_uid → new_uid），返回对账报告。

    引擎行 uid 改绑 + 池/能力/任务/消息/会话 uid 轨道同步，不留孤儿；缺列/缺表
    （未迁移旧库、历史 chat 专用库缺列）容错降级跳过。old_uid == new_uid 或任一
    为空 → no-op（幂等）。游标由调用方给，保证与调用点同事务。
    """
    report = {"engines_rebased": 0, "pool_rows": 0, "capability_rows": 0,
              "task_rows": 0, "task_message_rows": 0,
              "devops_sessions": 0, "chat_sessions": 0}
    old_uid = (old_uid or "").strip()
    new_uid = (new_uid or "").strip()
    if not old_uid or not new_uid or old_uid == new_uid:
        return report
    try:
        report["engines_rebased"] = rename_runtime_engine_uid(old_uid, new_uid)
    except Exception as _e:  # noqa: BLE001
        log.warning(f"rebase_engine_uid_references agent_runtimes: {_e}")
    for tbl, key in _ENGINE_UID_REF_TABLES:
        if not columns_exist(cursor, tbl, ("engine_uid",)):
            continue
        try:
            report[key] = cursor.execute(
                f"UPDATE {tbl} SET engine_uid=? WHERE engine_uid=?",
                (new_uid, old_uid)).rowcount
        except Exception as _e:  # noqa: BLE001
            log.warning(f"rebase_engine_uid_references {tbl}: {_e}")
    log.info("Issue #868 uid锚定改绑报告: " + json.dumps(report, ensure_ascii=False))
    return report


# ── 跨域常量再导出（2026-11-18）────────────────────────────────────────────
# insight 的告警求值（alerts.py / metric_rollup.py）要按 runtime 的失败判定口径
# 识别超时任务。此前两个文件各自 from ...engine.task_timeout import 这个字面量，
# 属跨域 import 别域内部模块（QA 顶层硬耦合清理项）。改为由 owner 域 public 提供，
# 调用方只依赖契约面；常量本体仍只有 task_timeout.py 一份，不复制。
def timeout_fail_signature() -> str:
    """超时失败的判定签名（供外域告警求值按同一口径识别超时任务）。

    实现在 runtime.engine.task_timeout.TIMEOUT_FAIL_SIGNATURE。以函数形式暴露而不是
    直接再导出常量：public 面只提供可调用入口（check_lib_query_names 亦按 def 登记）。
    """
    from src.domains.runtime.engine.task_timeout import TIMEOUT_FAIL_SIGNATURE as _sig

    return _sig

# ── 认证依赖与任务解析原语（2026-11-18）──────────────────────────────────────
# insight 的日志接入端点与 mgmt 的 autopilot 视图此前直接 import runtime 内部：
#   from src.domains.runtime.routes.runtime import _require_credential
#   from src.domains.runtime.engine.scheduler import _get_exec_agent_task
# 属跨域引内部实现（QA 顶层硬耦合清理项）。改为由 owner 域 public 提供入口。

def require_credential(request: Request) -> int:
    """FastAPI 依赖：解析 Authorization 的 runtime 凭据，未授权抛 401。

    转发到 runtime.routes.runtime._require_credential（唯一实现），
    供 insight 等外域的端点直接用作 `Depends(...)`。

    ⚠️ 形参注解 `Request` 是契约的一部分，不可省略（本模块 `from __future__ import
    annotations` 下注解为字符串，FastAPI 靠模块全局名解析）：缺注解时 FastAPI 会把
    `request` 当成**必填 query 参数**，端点对任何请求都先返回 422（凭据校验根本
    没跑到），insight 的 POST /api/obs/logs/ingest 全量 422 即此故障形态。
    返回值为 runtime_credentials.id（int），未授权由实现抛 HTTPException(401)。
    """
    from src.domains.runtime.routes.runtime import _require_credential as _impl

    return _impl(request)


def exec_agent_task(exec_id, agent_task_id: str = "", started_at: str = "",
                    created_at: str = "") -> dict | None:
    """按执行记录解析对应 agent_tasks 行（返回 dict，不传游标 —— 符合 public 约定）。

    内部实现需要游标，这里自建连接调用后转成 dict 返回，调用方不再接触游标。
    关联语义见 runtime.engine.scheduler._get_exec_agent_task：
    agent_task_id 非空按主键直查，为空走受约束兜底。
    """
    from src.domains.runtime.engine.scheduler import _get_exec_agent_task as _impl
    # 与内部实现同源：_get_exec_agent_task 原本就吃调用方游标，历史上调用方一直用
    # 进程库（session_manager.db）。这里同样用进程库，保证 agent_tasks 在同一库内解析
    # （曾经先试 db_registry.connection_for("runtime")，在单库测试环境会连到另一个库、
    # 查不到数据而返回 None —— 契约测试随即红）。
    from src.kernel.session_manager import db as _conn

    cur = _conn.cursor()
    row = _impl(cur, exec_id, agent_task_id, started_at, created_at)
    if not row:
        return None
    cols = [d[0] for d in (cur.description or [])]
    return dict(zip(cols, row)) if cols else None

# ── DevStage 云开发环境客户端（2026-11-18）──────────────────────────────────
# mgmt 的 /api/devstage 路由此前直接 `from src.domains.runtime.im.devstage_client
# import (...)` 一整套内部符号（含 _resolve_base_url / _request 两个私有函数），
# 属跨域 import 别域内部实现。改为由 owner 域 public 暴露最小集合。
#
# 注：该客户端的唯一消费方其实是 mgmt 路由本身，理想归宿是随功能迁到 mgmt 域；
# 在迁移前先用 public 收敛依赖方向（mgmt → runtime.public，而不是 → runtime.im.*）。
from src.domains.runtime.im.devstage_client import (  # noqa: E402
    DevStageError as DevStageError,
    _request as devstage_request,
    _resolve_base_url as devstage_base_url,
    list_environments as devstage_list_environments,
    create_environment as devstage_create_environment,
    start_environment as devstage_start_environment,
    stop_environment as devstage_stop_environment,
    delete_environment as devstage_delete_environment,
    list_specs as devstage_list_specs,
)

# ── 接管（takeover）服务门面（2026-11-18）──────────────────────────────────
# mgmt 的 /api/takeover 路由此前 `from src.domains.runtime.engine import takeover as svc`，
# 直接拿到 runtime 的内部服务模块并调用 12 个成员（含 _now_iso 私有函数），
# 属跨域 import 别域内部实现。这里按成员逐个转发，收敛依赖方向为
# mgmt → runtime.public（实现仍只有 runtime.engine.takeover 一份）。
from src.domains.runtime.engine import takeover as _takeover_impl  # noqa: E402

TakeoverError = _takeover_impl.TakeoverError


def takeover_active_status() -> str:
    """接管会话的「活跃」状态字面量（外域查询条件用，避免各写一份）。"""
    return _takeover_impl.ST_ACTIVE


def now_iso() -> str:
    """UTC ISO 时间戳，与 takeover 内部同一口径。"""
    return _takeover_impl._now_iso()


def same_user(sess_user_id, user: dict) -> bool:
    """接管会话归属校验（uuid / 历史整数 id 统一折算成字符串比较）。

    原路由里写 `int(sess["user_id"]) != int(user["id"])` —— D105 后账号 id 是 uuid，
    每次调用都 ValueError → 接管端点 500。判定实现归 runtime 域，外域只调这个门面。
    """
    return _takeover_impl._same_user(sess_user_id, user)
abort_takeover = _takeover_impl.abort_takeover
append_takeover_messages = _takeover_impl.append_takeover_messages
attach_client_session = _takeover_impl.attach_client_session
finish_takeover = _takeover_impl.finish_takeover
get_session = _takeover_impl.get_session
list_takeovers = _takeover_impl.list_takeovers
renew_takeover_session = _takeover_impl.renew_takeover_session
request_finish = _takeover_impl.request_finish
start_takeover = _takeover_impl.start_takeover
# mgmt 路由的 list / get 端点会调用这两个（「接管中」卡片上的 skipped_dispatches 计数）；
# 漏转发时 svc.skipped_dispatch_count 抛 AttributeError → GET /api/takeover/sessions
# 与 GET /api/takeover/sessions/{id} 恒 500（issue-3201 引入的守门计数）。
skipped_dispatch_count = _takeover_impl.skipped_dispatch_count
skipped_dispatch_message = _takeover_impl.skipped_dispatch_message

# ── 引擎实例 / 选择 / 健康派生门面（2026-11-18）──────────────────────────────
# 外域（mgmt 路由与 user.db、insight 看板与告警）此前直接
#   from src.domains.runtime.engine.instances import instance_name
#   from src.domains.runtime.engine.engine_select import query_engines
#   from src.domains.runtime.engine.health import derive_runtime_health
# 逐个 import runtime 的内部实现模块（QA 跨域 import 清理项）。
# 这里按成员转发，依赖方向收敛为 外域 → runtime.public；实现仍只有 engine.* 一份。
# 转发体一律函数内 import：engine.* 在加载期会反向引用别域 public，顶层 import 会成环。


def canonical_engine(engine_name: str | None) -> str:
    """由引擎实例名推导规范引擎键（前缀最长匹配；未知 → opencode）。"""
    from src.domains.runtime.engine.instances import canonical_engine as _impl

    return _impl(engine_name)


def usage_retry_engine(engine_name: str | None) -> bool:
    """该引擎 usage 为空时是否值得短重试一次。"""
    from src.domains.runtime.engine.instances import usage_retry_engine as _impl

    return _impl(engine_name)


def to_display(name: str) -> str:
    """API 出口统一显示名转换（内部名 → 展示名）。"""
    from src.domains.runtime.engine.instances import to_display as _impl

    return _impl(name)


def engine_mounts_to_credential(credential_id, host_name: str | None,
                                cred_device_name: str | None) -> bool:
    """引擎是否挂载到某凭据的设备（与设备视图挂载逻辑同一口径）。"""
    from src.domains.runtime.engine.instances import engine_mounts_to_credential as _impl

    return _impl(credential_id, host_name, cred_device_name)


def instance_name(engine_type: str, device_name: str, credential_id) -> str:
    """派生唯一引擎实例名（`{display}-{device_suffix}`）。"""
    from src.domains.runtime.engine.instances import instance_name as _impl

    return _impl(engine_type, device_name, credential_id)


def resolve_instance_id(engine_name: str, scope: str | None = None) -> int | None:
    """按唯一引擎名解析 agent_runtimes.id。"""
    from src.domains.runtime.engine.instances import resolve_instance_id as _impl

    return _impl(engine_name, scope)


def scope_of_runtime(rid: int) -> str | None:
    """返回 runtime 行所属 scope（user/repo/global）。"""
    from src.domains.runtime.engine.instances import scope_of_runtime as _impl

    return _impl(rid)


def known_agent_roles() -> set[str]:
    """合法节点角色集合（KNOWN_AGENT_ROLES 的可调用门面，避免再导出常量）。"""
    from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES as _roles

    return set(_roles)


def node_agent_role(node) -> str:
    """解析节点角色名（agent_type）——id 优先、tag 兜底。"""
    from src.domains.runtime.engine.engine_select import node_agent_role as _impl

    return _impl(node)


def node_matches_agent_role(node, role) -> bool:
    """节点是否匹配指定角色——id 优先、tag 兜底。"""
    from src.domains.runtime.engine.engine_select import node_matches_agent_role as _impl

    return _impl(node, role)


def selector_candidate_engines(selector: dict, agent_type: str, repo_key: str = "",
                               user_id: int = 0) -> tuple:
    """selector 候选集 + 分层排除原因（入队即时校验）。"""
    from src.domains.runtime.engine.engine_select import selector_candidate_engines as _impl

    return _impl(selector, agent_type, repo_key, user_id)


def validate_selector_tags(tags, scene_values=None) -> str:
    """保存端 L1 语法校验（服务端为权威）。"""
    from src.domains.runtime.engine.engine_select import validate_selector_tags as _impl

    return _impl(tags, scene_values)


def load_engine_selection() -> dict:
    """读引擎选择配置；缺失/损坏回默认。"""
    from src.domains.runtime.engine.engine_select import load_engine_selection as _impl

    return _impl()


def engines_by_ids(engine_ids: list[int], repo: str, cfg: dict) -> list[dict]:
    """按 runtime_id 列表查引擎详情（含 scope/priority）。"""
    from src.domains.runtime.engine.engine_select import _engines_by_ids as _impl

    return _impl(engine_ids, repo, cfg)


def norm_repo(repo_key: str) -> str:
    """仓库标识归一化为 owner/repo。"""
    from src.domains.runtime.engine.engine_select import _norm_repo as _impl

    return _impl(repo_key)


def get_scene_engine_selector(scene_id: str, agent_type: str, repo_key: str = "",
                             user_id: int = 0) -> dict | None:
    """读场景 node 的 engine_selector。"""
    from src.domains.runtime.engine.engine_select import get_scene_engine_selector as _impl

    return _impl(scene_id, agent_type, repo_key, user_id)


def get_scene_pool_engine_ids(scene_id: str, agent_type: str, repo_key: str = "",
                              user_id: int = 0, issue_creator: str = "") -> str | None:
    """限定广播引擎列表（JSON 字符串，供 pool_engine_ids 使用）。"""
    from src.domains.runtime.engine.engine_select import get_scene_pool_engine_ids as _impl

    return _impl(scene_id, agent_type, repo_key, user_id, issue_creator)


def resolve_default_engine(repo_key: str, agent_type: str = "") -> int | None:
    """托管默认选择：专属 → 全局 → 本机直连。"""
    from src.domains.runtime.engine.engine_select import resolve_default_engine as _impl

    return _impl(repo_key, agent_type)


def query_engines(repo_key: str, scope: str, agent_type: str = "",
                  username: str = "") -> list[dict]:
    """某仓库在某档位的候选引擎（repo/global/local）。"""
    from src.domains.runtime.engine.engine_select import query_engines as _impl

    return _impl(repo_key, scope, agent_type, username)


def cleanup_scene_engine_bindings(rid_list: list, commit: bool = True) -> int:
    """从所有活跃场景 nodes_json 中移除对 rid_list 引擎的绑定。"""
    from src.domains.runtime.engine.engine_select import cleanup_scene_engine_bindings as _impl

    return _impl(rid_list, commit)


def get_detached_engine_hints(scene_row_id: int = 0, runtime_ids: list | None = None,
                              repo_key: str = "") -> list:
    """查询场景/引擎的被解绑提示信息。"""
    from src.domains.runtime.engine.engine_select import get_detached_engine_hints as _impl

    return _impl(scene_row_id, runtime_ids, repo_key)


def explain_engine_selection(repo_key: str, agent_type: str, scene_id: str = "",
                             user_id: int = 0, issue_creator: str = "") -> dict:
    """解释某 (repo, agent_type, scene) 的引擎候选与过滤全景。"""
    from src.domains.runtime.engine.engine_select import explain_engine_selection as _impl

    return _impl(repo_key, agent_type, scene_id, user_id, issue_creator)


def derive_runtime_health(status: str, last_seen_at: str | None,
                          now: float | None = None) -> str:
    """在线态派生的唯一入口（status 为 agent_runtimes 在线态真源）。"""
    from src.domains.runtime.engine.health import derive_runtime_health as _impl

    return _impl(status, last_seen_at, now)


# 原名别名：调用点只改模块路径，函数名保持不变（改动面最小）
_engines_by_ids = engines_by_ids
_norm_repo = norm_repo


# ── 仓库平台卡 / VCS 客户端门面（2026-11-18）─────────────────────────────────
# 外域（mgmt 路由与 user 域、contracts 本地适配）此前逐个
#   from src.domains.runtime.vcs.platform_card import get_platform_card
#   from src.domains.runtime.vcs.api_client import VcsApiClient
# 直接 import runtime 内部实现模块（QA 跨域 import 清理项）。
# 依赖方向收敛为 外域 → runtime.public；实现仍只有 vcs.* 一份。
# 转发体一律函数内 import：vcs 内部模块在加载期会反向引用 mgmt.public，顶层 import 会成环。


def get_platform_card(provider: str):
    """按 provider 取平台卡快照（未注册 → None；内存缓存 TTL 300s）。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card as _impl

    return _impl(provider)


def list_vcs_platforms() -> list[str]:
    """已注册（内置默认 + 表配置）的平台 provider 列表。"""
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms as _impl

    return _impl()


def invalidate_platform_card_cache() -> None:
    """主动失效平台卡内存缓存（表配置写入后调用）。"""
    from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache as _impl

    return _impl()


def build_platform_card(provider: str, raw: dict):
    """由原始配置构建并校验平台卡（非法配置抛 PlatformCardError）。"""
    from src.domains.runtime.vcs.platform_card import build_platform_card as _impl

    return _impl(provider, raw)


def card_field_overrides(submitted: dict, card) -> dict:
    """管理面提交值与当前生效卡的字段差异（A2 覆盖层，只记变化过的键）。"""
    from src.domains.runtime.vcs.platform_card import card_field_overrides as _impl

    return _impl(submitted, card)


def compute_card_overrides(submitted: dict, current_json: str = "") -> dict:
    """管理面保存后该记的覆盖层（契约）：{provider: 提交整卡} + 现有 meta → 新覆盖。"""
    from src.domains.runtime.vcs.card_overrides import CardOverrides
    from src.domains.runtime.vcs.platform_card import base_platform_card as _base

    return CardOverrides.from_json(current_json).recompute(submitted, _base).by_provider


def platform_card_overrides_meta_key() -> str:
    """覆盖层 meta 契约键（调用方不必硬编码字符串）。"""
    from src.domains.runtime.vcs.card_overrides import META_KEY

    return META_KEY


def base_platform_card(provider: str):
    """不含管理面覆盖层的卡（管理面保存时的 diff 基线）。"""
    from src.domains.runtime.vcs.platform_card import base_platform_card as _impl

    return _impl(provider)


def module_for(provider: str):
    """取 provider 对应的 VCS 后端模块（providers 注册表口径）。"""
    from src.domains.runtime.vcs.providers import module_for as _impl

    return _impl(provider)


def pr_web_path(provider: str) -> str:
    """provider 模块声明的 PR 网页路径（能力位缺失时回落 "pull"）。"""
    from src.domains.runtime.vcs.providers import _MODULES as _mods

    return getattr(_mods.get(provider), "PR_WEB_PATH", "pull") or "pull"


def hosting_full_flow_providers() -> list[str]:
    """声明支持托管全流程（HOSTING_FULL_FLOW）的 provider 列表。"""
    from src.domains.runtime.vcs.providers import _MODULES as _mods

    return sorted(p for p, m in _mods.items() if getattr(m, "HOSTING_FULL_FLOW", False))


def default_vcs_provider() -> str:
    """内置默认平台（平台卡声明序首）；不可用时返回空串。"""
    try:
        from src.domains.runtime.vcs.platform_card import _DEFAULT_CARDS_RAW as _raw

        return next(iter(_raw), "")
    except Exception:
        return ""


def native_issue_number(value):
    """issue 编号保持平台原生形态（int 平台返回 int；字符串 ident 平台返回 str）。"""
    from src.domains.runtime.vcs.api_client import _native_issue_number as _impl

    return _impl(value)


# 原名别名：调用点只改模块路径，函数名保持不变（改动面最小）
_native_issue_number = native_issue_number


# 对象存储 / 技能包缓存面：跨域调用方（mgmt、contracts）只能用这层转发，
# 不直接触达 runtime.storage 内部模块（storage 在加载期反向引用别域 public，顶层 import 会成环）
def get_storage():
    """取对象存储后端（未配置返回 None）。"""
    from src.domains.runtime.storage import get_storage as _impl

    return _impl()


def get_skill_storage():
    """取技能包对象存储后端（未配置返回 None）。"""
    from src.domains.runtime.storage import get_skill_storage as _impl

    return _impl()


def skill_key(scope: str, owner: str, skill_name: str, version: int | None = None,
              rand8: str | None = None) -> str:
    """生成技能 zip 对象键（与 storage 同一口径）。"""
    from src.domains.runtime.storage import skill_key as _impl

    return _impl(scope, owner, skill_name, version, rand8)


def zip_cache_get(object_key: str):
    """读磁盘 zip 缓存字节（未命中返回 None）。"""
    from src.domains.runtime.storage import zip_cache_get as _impl

    return _impl(object_key)


def zip_cache_get_with_hash(object_key: str):
    """读磁盘 zip 缓存字节 + 内容哈希 + skill_id（未命中返回 None）。"""
    from src.domains.runtime.storage import zip_cache_get_with_hash as _impl

    return _impl(object_key)


def zip_cache_put(object_key: str, data: bytes, content_hash: str = "",
                  skill_id: int = 0) -> None:
    """写入磁盘 zip 缓存（原子替换 + LRU 清理）。"""
    from src.domains.runtime.storage import zip_cache_put as _impl

    return _impl(object_key, data, content_hash, skill_id)


def mem_cache_invalidate(key: str) -> None:
    """失效单个内存缓存项。"""
    from src.domains.runtime.storage import mem_cache_invalidate as _impl

    return _impl(key)


def invalidate_skill_caches(object_key: str) -> None:
    """失效某个技能对象键相关的内存缓存。"""
    from src.domains.runtime.storage import invalidate_skill_caches as _impl

    return _impl(object_key)


def get_skill_zip_bytes(object_key: str, skill_name: str = ""):
    """取技能 zip 字节（缓存优先，回源后端）。"""
    from src.domains.runtime.storage import get_skill_zip_bytes as _impl

    return _impl(object_key, skill_name)


def skill_md_content(object_key: str, skill_name: str):
    """读技能包内 SKILL.md 文本（不存在返回 None）。"""
    from src.domains.runtime.storage import skill_md_content as _impl

    return _impl(object_key, skill_name)


def list_zip_sub_skills(zip_bytes: bytes, skill_name: str) -> list:
    """列出技能 zip 内的子技能。"""
    from src.domains.runtime.storage import list_zip_sub_skills as _impl

    return _impl(zip_bytes, skill_name)


def read_zip_sub_skill(zip_bytes: bytes, skill_name: str, sub_name: str):
    """读技能 zip 内指定子技能（不存在返回 None）。"""
    from src.domains.runtime.storage import read_zip_sub_skill as _impl

    return _impl(zip_bytes, skill_name, sub_name)


def rebuild_scene_skill_ref(market_id: int, nodes: list, scene_name: str = "",
                           repo_key: str = ""):
    """重建 scene_skill_ref（DELETE + INSERT）。"""
    from src.domains.runtime.storage.scene_sync import _rebuild_scene_skill_ref as _impl

    return _impl(market_id, nodes, scene_name, repo_key)


# 原名别名：调用点只改模块路径，函数名保持不变（改动面最小）
_rebuild_scene_skill_ref = rebuild_scene_skill_ref


# 值对象/客户端类：属性访问时再导入（顶层 import 会成环，类也无法用转发函数包住）
_LAZY_CLASS_ATTRS = {
    "PlatformCard": ("src.domains.runtime.vcs.platform_card", "PlatformCard"),
    "PlatformCardError": ("src.domains.runtime.vcs.platform_card", "PlatformCardError"),
    "VcsApiClient": ("src.domains.runtime.vcs.api_client", "VcsApiClient"),
    "UsageCollector": ("src.domains.runtime.engine.usage_collect", "UsageCollector"),
}


def __getattr__(name: str):
    """PEP 562 懒导出类：`from src.domains.runtime.public import VcsApiClient` 可用。"""
    target = _LAZY_CLASS_ATTRS.get(name)
    if not target:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])


# ── 引擎面（health / scorer / scheduler / executor / service）2026-11-18 ─────
# 外域（mgmt 路由与 user 域、insight 观测面）此前逐个
#   from src.domains.runtime.engine.<mod> import <sym>
# 直接 import runtime 内部实现模块（QA 跨域 import 清理项）。
# 依赖方向收敛为 外域 → runtime.public；实现仍只有 engine.* 一份。
# 转发体一律函数内 import：engine 内部模块在加载期会反向引用别域 public，顶层 import 会成环。


def runtime_credential_id(runtime_id: int):
    """取 runtime 绑定的凭据 id（无绑定 → None）。"""
    from src.domains.runtime.engine.service import runtime_credential_id as _impl

    return _impl(runtime_id)


def probe_aishell_dsh_acp(cli_path: str) -> dict:
    """探测 aishell-dsh ACP 引擎可用性。"""
    from src.domains.runtime.engine.agent import _probe_aishell_dsh_acp as _impl

    return _impl(cli_path)


_probe_aishell_dsh_acp = probe_aishell_dsh_acp


def probe_hermes_acp(cli_path: str) -> dict:
    """探测 hermes ACP 引擎可用性（initialize + session/new + prompt 三步）。

    单源：与 worker 侧同一实现。insight 的 engine_probe 曾自带一份只发
    initialize 的浅探活 —— provider 侧错误（模型不存在等）只在 prompt 阶段
    暴露，于是控制台一路显示 ok 而 worker 报 fail（实测 112 次 fail 对 0 次）。
    """
    from src.domains.runtime.engine.agent import _probe_hermes_acp as _impl

    return _impl(cli_path)


_probe_hermes_acp = probe_hermes_acp


def discover_backends() -> list[dict]:
    """枚举已注册的 Agent 执行后端（含 installed/cli_path 探测结果）。"""
    from src.domains.runtime.agents.executor import discover_backends as _impl

    return _impl()


# 原名别名：registry.discover_runtimes 即 executor.discover_backends 的 runtime 命名
discover_runtimes = discover_backends


def usage_payload(u: dict, engine: str = "") -> dict:
    """用量事件字段归一的唯一入口（worker 侧与观测侧同一份）。"""
    from src.domains.runtime.engine.agent import _usage_payload as _impl

    return _impl(u, engine)


_usage_payload = usage_payload


def get_health_score(runtime_id: int, force_refresh: bool = False) -> dict:
    """runtime 健康分（缺省走 TTL 缓存）。"""
    from src.domains.runtime.engine.health_scorer import get_health_score as _impl

    return _impl(runtime_id, force_refresh)


def reset_health(runtime_id: int) -> dict | None:
    """清空指定 runtime 的健康分累计。"""
    from src.domains.runtime.engine.health_scorer import reset_health as _impl

    return _impl(runtime_id)


def derive_local_runtime_health(installed: bool, healthy: bool) -> str:
    """本机引擎在线态派生（无 credential 的本地引擎）。"""
    from src.domains.runtime.engine.health import derive_local_runtime_health as _impl

    return _impl(installed, healthy)


def parse_dt(s: str) -> float | None:
    """解析 runtime 时间戳为 epoch 秒（不可解析 → None）。"""
    from src.domains.runtime.engine.health import parse_dt as _impl

    return _impl(s)


def apply_runtime_state(c, runtime_id: int, status: str, last_seen_at: str) -> None:
    """写 agent_runtimes 在线态（owner 域原语，调用方传自己的游标）。"""
    from src.domains.runtime.engine.health import apply_runtime_state as _impl

    return _impl(c, runtime_id, status, last_seen_at)


def aggregate_skill_consistency(days: int = 7) -> dict:
    """技能一致性聚合（观测面看板用）。"""
    from src.domains.runtime.engine.health import aggregate_skill_consistency as _impl

    return _impl(days)


def recover_engine(runtime_id: int, trigger: str = "manual", force: bool = False) -> dict:
    """engine 重挂载（readd）。"""
    from src.domains.runtime.engine.recovery import recover_engine as _impl

    return _impl(runtime_id, trigger, force)


def probe_local_engines(engines: list | None = None) -> list[dict]:
    """本机引擎探测（缺省枚举全部已注册后端）。"""
    from src.domains.runtime.engine.local_probe import probe_local_engines as _impl

    return _impl(engines)


def scan_local_skills() -> list[dict]:
    """扫描本机已装技能列表。"""
    from src.domains.runtime.engine.agent import _scan_local_skills as _impl

    return _impl()


def scan_local_skills_with_detail(engine_type: str) -> list[dict]:
    """扫描本机技能并带明细（按引擎类型）。"""
    from src.domains.runtime.engine.agent import _scan_local_skills_with_detail as _impl

    return _impl(engine_type)


_scan_local_skills = scan_local_skills
_scan_local_skills_with_detail = scan_local_skills_with_detail


def get_execution_mode() -> str:
    """当前执行模式（auto/direct/queue_remote）。"""
    from src.domains.runtime.engine.service import get_execution_mode as _impl

    return _impl()


def set_execution_mode(mode: str):
    """切换执行模式（owner 域校验 + 落库）。"""
    from src.domains.runtime.engine.service import set_execution_mode as _impl

    return _impl(mode)


def load_project_info(repo_key: str) -> tuple[dict, dict]:
    """取仓库 project-info（原始 + 派生）。"""
    from src.domains.runtime.engine.service import load_project_info as _impl

    return _impl(repo_key)


def detect_engine_model(name: str) -> str:
    """按引擎名推断默认模型。"""
    from src.domains.runtime.engine.engine_model import detect_engine_model as _impl

    return _impl(name)


async def trigger_task_now(task_id: int) -> dict:
    """立即触发一次任务调度（scheduler 单一入口）。"""
    from src.domains.runtime.engine.scheduler import trigger_task_now as _impl

    return await _impl(task_id)


def seed_official_skill_if_missing(skill_name: str, display_name: str, description: str,
                                   agent_type: str, skill_path: str) -> bool:
    """启动期补齐官方技能行（幂等；skill 表归 runtime 域）。"""
    from src.domains.runtime.storage.skill_sync import seed_official_skill_if_missing as _impl

    return _impl(skill_name, display_name, description, agent_type, skill_path)


async def trigger_task_now_with_params(task_id: int, params: dict | None = None) -> dict:
    """立即触发一次任务调度，并把本次参数注入 prompt（组织 AI 分析单次运行）。

    scheduler.trigger_task_now 是 async；本函数供 mgmt 域通过 public 调用。
    """
    from src.domains.runtime.engine.scheduler import trigger_task_now as _impl

    return await _impl(task_id, params or {})


def normalize_dedup_level(raw) -> str:
    """去重级别归一。"""
    from src.domains.runtime.engine.scheduler import normalize_dedup_level as _impl

    return _impl(raw)


def sanitize_dedup_note(raw) -> str:
    """去重备注清洗。"""
    from src.domains.runtime.engine.scheduler import sanitize_dedup_note as _impl

    return _impl(raw)


def sanitize_dedup_confirmed_by(raw) -> str:
    """去重确认人清洗。"""
    from src.domains.runtime.engine.scheduler import sanitize_dedup_confirmed_by as _impl

    return _impl(raw)


def dedup_auto_merge_allowed(dedup_level, dedup_confirmed: bool = False) -> bool:
    """自动合并门禁判定。"""
    from src.domains.runtime.engine.scheduler import dedup_auto_merge_allowed as _impl

    return _impl(dedup_level, dedup_confirmed)


def dedup_level_suspected() -> str:
    """疑似重复级别常量（常量不外露，包成函数以满足 lib 门禁登记口径）。"""
    from src.domains.runtime.engine.scheduler import DEDUP_LEVEL_SUSPECTED

    return DEDUP_LEVEL_SUSPECTED


def columns_exist(cursor, table: str, cols: tuple) -> bool:
    """判断表的列是否齐备（cursor 由调用方提供）。"""
    from src.domains.runtime.engine.refs_reconcile import columns_exist as _impl

    return _impl(cursor, table, cols)


def session_uid_mirror_sql(tbl: str) -> str:
    """session uid 镜像 SQL 片段（SQL 归 owner 域产出）。"""
    from src.domains.runtime.engine.refs_reconcile import session_uid_mirror_sql as _impl

    return _impl(tbl)


def reconcile_for_caller(caller_cursor, runtime_ids: list, keep_id: int, reason: str = "") -> dict:
    """按调用方游标做 runtime 引用归并。"""
    from src.domains.runtime.engine.refs_reconcile import reconcile_for_caller as _impl

    return _impl(caller_cursor, runtime_ids, keep_id, reason)


def backend_registered(name: str) -> bool:
    """后端是否已注册（替代直接 import 注册表对象 _BACKENDS）。"""
    from src.domains.runtime.agents.executor import _BACKENDS

    return name in _BACKENDS


# ── IM 渠道安装 / 引擎 / 通知门面（2026-11-18 跨域 import 批次 runtime.im）──────
# 外域（mgmt 路由 im/agents/feishu_onboard/feishu_card/settings，insight 告警）此前
# 直接 import runtime.im 的内部模块：
#   from src.domains.runtime.im.engine_wiring import _connect, default_db_path, ...
#   from src.domains.runtime.im import outbox as OB / notify as _notify
#   from src.domains.runtime.im.adapters.dingtalk.cards import DingtalkCardClient ...
# 依赖方向改为 外域 → runtime.public；实现仍只有 runtime.im 一份。
# 转发体一律函数内 import：runtime.im.* 在加载期会反向引用别域 public，顶层 import 成环。
#
# 注：grape_t_channel_installation 归属 mgmt（configs/table_ownership.json），但该表实际
# 落在 IM 共享域库（GRAPE_IM_DB_NAME 钉库），只有 engine_wiring 的连接解析知道目标库，
# 因此这里暴露 im_db_connection() 让 mgmt 用同一连接做同库读写，不另起连接猜测库名。


def im_db_connection():
    """IM 共享域库连接（跨进程同库口径；调用方负责 close()）。

    返回连接而非行对象：调用方需要在同库内做「查-改-提交」事务（安装行归属 mgmt），
    public 无法在不引入跨库事务的前提下替它包成一个纯查询函数。
    """
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path as _path

    return _connect(_path())


def engine_mode() -> str:
    """IM 引擎模式（on | off，data/im_engine_mode 优先）。"""
    from src.domains.runtime.im.engine_wiring import engine_mode as _impl

    return _impl()


def feishu_api(app_id: str = ""):
    """按 app_id 取（进程内缓存的）飞书 Async 客户端。"""
    from src.domains.runtime.im.engine_wiring import feishu_api as _impl

    return _impl(app_id)


def get_active_installation(scope: str = "user", *, owner_user_id: str = "", repo_key: str = "",
                            channel_type: str = "", app_id: str = "") -> dict | None:
    """读 IM 渠道活跃安装（返回 dict，不含游标）；无则 None。"""
    from src.domains.runtime.im.engine_wiring import get_active_installation as _impl

    return _impl(scope, owner_user_id=owner_user_id, repo_key=repo_key,
                 channel_type=channel_type, app_id=app_id)


def sync_installation(scope: str, *, owner_user_id: str = "", repo_key: str = "",
                      app_id: str = "", app_secret: str = "", channel_type: str = "feishu",
                      extra_config: dict | None = None) -> bool:
    """UI 保存/换 app 的安装写入口（幂等 upsert，active）。"""
    from src.domains.runtime.im.engine_wiring import sync_installation as _impl

    return _impl(scope, owner_user_id=owner_user_id, repo_key=repo_key, app_id=app_id,
                 app_secret=app_secret, channel_type=channel_type, extra_config=extra_config)


def revoke_installation(scope: str, *, owner_user_id: str = "", repo_key: str = "",
                        app_id: str = "", channel_type: str = "feishu") -> bool:
    """UI 解绑/换 app：安装行置 revoked。"""
    from src.domains.runtime.im.engine_wiring import revoke_installation as _impl

    return _impl(scope, owner_user_id=owner_user_id, repo_key=repo_key, app_id=app_id,
                 channel_type=channel_type)


def resolve_engine():
    """本进程 IM 引擎（4 进程口径：只有持有引擎的进程返回实例，其余 None）。"""
    from src.domains.runtime.im.notify import resolve_engine as _impl

    return _impl()


def notify_stats() -> dict:
    """通知扇出计数（全渠道）。"""
    from src.domains.runtime.im.notify import stats as _impl

    return _impl()


async def notify_user(user_id: int, kind: str, text: str, *, repo_key: str = "",
                      issue_number: int = 0, conv_id: str = "", targets=None,
                      engine=None, dedupe: bool = True) -> dict:
    """主动通知该用户的全部已连接渠道（每渠独立成败）。"""
    from src.domains.runtime.im.notify import notify_user as _impl

    return await _impl(user_id, kind, text, repo_key=repo_key, issue_number=issue_number,
                       conv_id=conv_id, targets=targets, engine=engine, dedupe=dedupe)


def schedule_notify(user_id, kind: str, text: str, **kw) -> bool:
    """通知（跨域契约：其他域只能经本函数发站内信/IM）。

    Issue #3469 F：邮箱不可投递时的降级通道。有事件循环时 fire-and-forget，
    没有时**同步入队**；返回值表示是否真的产生了通知动作（调用方不得再假定成功）。
    """
    from src.domains.runtime.im.notify import schedule_notify as _impl

    return bool(_impl(user_id, kind, text, **kw))


def outbox_counts() -> dict:
    """通知 outbox 计数（按状态）。"""
    from src.domains.runtime.im.outbox import counts as _impl

    return _impl()


def outbox_recent_undelivered(limit: int = 20) -> list:
    """最近未送达的 outbox 行（dict 列表）。"""
    from src.domains.runtime.im.outbox import recent_undelivered as _impl

    return _impl(limit=limit)


def outbox_list_rows(status: str = "", limit: int = 50) -> list:
    """outbox 明细（可按状态筛，倒序）。"""
    from src.domains.runtime.im.outbox import list_rows as _impl

    return _impl(status=status, limit=limit)


def outbox_get_row(out_track_id: str) -> dict | None:
    """按 out_track_id 取 outbox 行。"""
    from src.domains.runtime.im.outbox import get_row as _impl

    return _impl(out_track_id)


def outbox_requeue_for_retry(out_track_id: str) -> bool:
    """手动重发：failed/expired → pending。"""
    from src.domains.runtime.im.outbox import requeue_for_retry as _impl

    return _impl(out_track_id)


def dingtalk_card_client(app_id: str, app_secret: str):
    """构造钉钉互动卡片实例客户端。"""
    from src.domains.runtime.im.adapters.dingtalk.cards import DingtalkCardClient as _C

    return _C(app_id, app_secret)


def new_out_track_id(seed: str = "") -> str:
    """生成 out_track_id（跨进程幂等键）。"""
    from src.domains.runtime.im.adapters.dingtalk.cards import new_out_track_id as _impl

    return _impl(seed)


def set_card_mode_file(enabled: bool) -> str:
    """写钉钉卡片模式开关文件，返回写入值。"""
    from src.domains.runtime.im.adapters.dingtalk.cards import set_card_mode_file as _impl

    return _impl(enabled)


def installation_fingerprint(cfg: dict) -> str:
    """安装 config 指纹（与 IM 引擎扫描同一口径）。"""
    from src.domains.runtime.im.engine.supervisor import fingerprint as _impl

    return _impl(cfg)


def parse_card_action(event_data: dict) -> dict | None:
    """解析飞书卡片回调（兼容直接 POST 与 WS 两种格式）。"""
    from src.domains.runtime.im.feishu_async import _parse_card_action as _impl

    return _impl(event_data)


def feishu_client():
    """构造飞书 Async 客户端（调用方自行 close）。"""
    from src.domains.runtime.im.feishu_async import FeishuAsync as _C

    return _C()


def bot_context(app_id: str = "", app_secret: str = "", bot_type: str = "chat_bot", name: str = ""):
    """构造单 Bot 隔离上下文。"""
    from src.domains.runtime.im.feishu_async import BotContext as _C

    return _C(app_id=app_id, app_secret=app_secret, bot_type=bot_type, name=name)


def set_gitcode_nickname(user_id, nickname: str) -> None:
    """写 GitCode 昵称（channel_directory 单一权威访问层）。"""
    from src.domains.runtime.im.channel_directory import set_gitcode_nickname as _impl

    return _impl(user_id, nickname)


# ── 云沙箱 adapter / 引擎注册表只读面（mgmt 管理面消费，2026-11-18 跨域收敛）──
# 管理面（mgmt.routes.settings / sandbox_admin）原来直接 import runtime.agents.sandbox.*
# 的内部模块。这里给等价原语，转发体一律函数内 import（内部模块加载期会反向引用别域）。
def get_cloud_registry() -> dict:
    """云沙箱平台 → Provider 类映射。"""
    from src.domains.runtime.agents.sandbox.backends import get_cloud_registry as _impl

    return _impl()


def get_adapter(platform: str):
    """取平台 adapter 实例；未注册平台返回 None。"""
    from src.domains.runtime.agents.sandbox.backends import get_adapter as _impl

    return _impl(platform)


def get_all_adapters() -> list:
    """全部 adapter 实例（本地 + 云）。"""
    from src.domains.runtime.agents.sandbox.backends import get_all_adapters as _impl

    return _impl()


def get_cloud_platforms() -> list[str]:
    """支持的云沙箱平台名列表。"""
    from src.domains.runtime.agents.sandbox.backends import get_cloud_platforms as _impl

    return _impl()


def default_registry() -> Any:
    """平台 → Provider 工厂映射（只读快照）。"""
    from src.domains.runtime.agents.sandbox.backends import default_registry as _impl

    return _impl()


def engine_types() -> list[str]:
    """云沙箱支持的引擎类型（ENGINE_REGISTRY 的键，保持注册顺序）。"""
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY

    return list(ENGINE_REGISTRY.keys())


def has_engine_type(engine_type: str) -> bool:
    """引擎类型是否已注册（严格比对，不含 "common" 兜底）。"""
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY

    return engine_type in ENGINE_REGISTRY


def engine_plugins_info() -> list[dict]:
    """引擎插件摘要（JSON 友好）：engine_type / display_name / installable。"""
    from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY

    return [
        {
            "engine_type": et,
            "display_name": et,
            "installable": bool(getattr(plugin.installer, "install_cmd", "")),
        }
        for et, plugin in sorted(ENGINE_REGISTRY.items())
    ]


def invalidate_cache(backend_name: str | None = None) -> None:
    """失效沙箱网络访问规则缓存（白名单 CRUD 后调用）。"""
    from src.domains.runtime.agents.sandbox.whitelist import invalidate_cache as _impl

    return _impl(backend_name)


def refresh_whitelist_cache() -> None:
    """刷新城墙域名白名单内存缓存（不直接暴露 _WhitelistCache 类）。"""
    from src.domains.runtime.agents.sandbox.isolation.network import _WhitelistCache

    _WhitelistCache.get().refresh()


def is_plugin_mode() -> bool:
    """当前管线模式是否为 plugin（plugin/legacy 二选一）。"""
    from src.domains.runtime.pipeline.event_pipeline import is_plugin_mode as _impl

    return _impl()


# ── 组织下派/上下文场景的跨域写口（A10/A11 整改，2026-11-19）──────────────
# mgmt 的组织编排需要终止子任务、重触发流水线、把正文写 OBS、复用代码仓
# git 工作副本。此前直接 import runtime 内部实现（issue_cancel / engine.service /
# event_pipeline / storage / engine.repogit 私有函数），跨域契约隐式化。
# 这里收敛为 public 具名入口，实现仍只有一份；调用方只依赖本模块。


def cancel_issue_core(issue_id, *, user_id=None):
    """取消一个 Issue 及其在途/未完成任务（与 runtime 内部同语义）。"""
    from src.domains.runtime.issue_cancel import cancel_issue_core as _impl

    return _impl(issue_id, user_id=user_id)


def cancel_task(task_id) -> None:
    """把指定 agent 任务置为 cancelled（引擎侧取消入口）。"""
    from src.domains.runtime.engine.service import cancel_task as _impl

    return _impl(task_id)


def cancel_direct_task(seq: int) -> bool:
    """取消排队/运行中的 direct 任务（按事件 seq）。"""
    from src.domains.runtime.pipeline.event_pipeline import cancel_direct_task as _impl

    return _impl(seq)


def run_pipeline(raw_body: bytes, headers: dict, account: str = "",
                 repo_key_override: str = "", replay: bool = False) -> dict:
    """事件处理管道入口（内部触发场景复用，Issue #2118 重试路径）。"""
    from src.domains.runtime.pipeline.event_pipeline import run_pipeline as _impl

    return _impl(raw_body, headers, account=account,
                 repo_key_override=repo_key_override, replay=replay)


def run_git(args: list, token: str = "", timeout: int = 900,
            scheme: str = "x_access_token"):
    """以平台鉴权方式执行 git 命令（返回 CompletedProcess 或 None）。"""
    from src.domains.runtime.engine.repogit import _run_git as _impl

    return _impl(args, token=token, timeout=timeout, scheme=scheme)


def clone_auth_scheme_for(repo_key: str) -> str:
    """仓库 clone 使用的鉴权注入方案标识（gitcode/github 各有口径）。"""
    from src.domains.runtime.engine.repogit import _clone_auth_scheme_for as _impl

    return _impl(repo_key)


def storage_save_bytes(obs_key: str, data: bytes) -> bool:
    """写对象存储；未配置存储后端时返回 False（调用方降级本地）。"""
    from src.domains.runtime.storage import get_storage

    storage = get_storage()
    if storage is None:
        return False
    storage.save_bytes(obs_key, data)
    return True


def storage_delete_object(obs_key: str) -> bool:
    """删除对象存储对象（幂等）；未配置存储后端时返回 False。"""
    from src.domains.runtime.storage import get_storage

    storage = get_storage()
    if storage is None:
        return False
    storage.delete_object(obs_key)
    return True
