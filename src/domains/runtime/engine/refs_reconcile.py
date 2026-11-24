"""runtime 域：引擎行删除/去重前的运行态引用对账（Issue #3097 第 6/7 条）。

**为什么在 runtime 域**：本例程碰的 6 张表（agent_runtimes / agent_tasks /
agent_type_runtime_pool / engine_capabilities / devops_agent_session /
chat_agent_session）**全部归 runtime 库**（见 `configs/table_ownership.json`）。
它原先长在 `src/domains/mgmt/user/db.py` 里，等于 mgmt 域直连 runtime 表——三库
拆分后每一条 SQL 都要靠 mgmt 连接去访问别域的表，既违反域边界，也让「表不存在」
变成难以观测的静默分支。

现在实现（判定逻辑 + SQL）都在本模块，mgmt 侧只保留薄接线（参数整形 + 委托），
调用方照旧传自己的游标（runtime 进程内就是 runtime 域的连接）。

**对账语义**（与 Issue #646 / #868 定稿一致，行为不变）：

- keep_id 非 0（改绑模式，去重路径）：把待删行的引用改绑到保留行——任务/会话/
  绑定表直接改绑；池/能力同键绑定已在保留行存在则删重复绑定，否则改绑
  （UNIQUE(agent_type,runtime_id)/UNIQUE(runtime_id,tag) 冲突安全），engine_uid
  镜像统一指向保留行 uid。
- keep_id=0（显式清理模式，删除路径）：池/能力绑定显式删除（runtime_id 或
  engine_uid 命中），会话 runtime_id 置 0、历史绑定表删除、场景 nodes_json
  engine_runtime_ids 悬空引用清理；agent_tasks 按快照语义保留（恢复走
  `/api/events/{seq}/redispatch` 重绑）。
- 未迁移旧库（缺 engine_uid 列）自动降级为 runtime_id 单轨。

**不再静默**（Issue #3097 第 7 条）：每一次「这一步没做成」都进两处——
`report["errors"]`（结构化，可断言/可对账）与一条 JSON 结构化 warn 日志
（`event=engine_refs_reconcile_step_failed`，带 table/action/error）。缺表/缺列
这类**预期内降级**同样记录，只是 action 标注为 skipped 语义，便于事后分辨
「本来就跳过」与「本该成功却失败」。
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# 结构化告警事件名（日志检索/告警规则按此聚合）
WARN_EVENT = "engine_refs_reconcile_step_failed"

# 表是否已具备指定列（未迁移旧库降级判定）的进程内缓存：按连接身份隔离，
# 避免不同库（单库/域库、测试临时库）之间互相污染。
_COLS_CACHE: dict = {}
_COLS_CACHE_CONN = None


def columns_exist(cursor, table: str, cols: tuple) -> bool:
    """表是否已具备指定列（PRAGMA table_info → MySQL information_schema）。

    「探不到」与「没有该列」等价处理（返回 False → 调用方降级为单轨），
    与 Issue #646 原实现一致；调用方把 False 记成结构化 warn，不再静默。
    """
    global _COLS_CACHE, _COLS_CACHE_CONN
    conn = getattr(cursor, "connection", None)
    conn_id = id(conn) if conn is not None else id(cursor)
    if _COLS_CACHE_CONN != conn_id:
        _COLS_CACHE = {}
        _COLS_CACHE_CONN = conn_id
    key = f"{table}:{','.join(cols)}"
    if key in _COLS_CACHE:
        return _COLS_CACHE[key]
    try:
        # 探列单一入口（kernel.ddl_safety）：PRAGMA（compat 翻译）+ 裸 MySQL
        # information_schema 回退；PR #2009 I2 收敛。
        from src.kernel.ddl_safety import table_columns

        present = set(table_columns(cursor, table))
        ok = all(col in present for col in cols)
    except Exception:  # noqa: BLE001  # 探列失败=降级单轨，调用方记结构化 warn
        ok = False
    _COLS_CACHE[key] = ok
    return ok


def session_uid_mirror_sql(tbl: str) -> str:
    """会话表 engine_uid 镜像对账 UPDATE（跨方言写法）。

    Issue #868 QA 复审：原 `engine_uid IS NOT (SELECT ...)` 是 SQLite 专用方言
    （MySQL 8.0 报 1064 且被吞错 → runtime_id 已改绑而 uid 残留 stale）。改为
    NULL-safe 比较：NULLIF 归一 NULL/''，与子查询 COALESCE 后 <> 比较，语义等价
    （NULL/''/stale 均命中、已一致行不重写、计数=实际矫正行数）；runtime 行不存在
    时 stale uid 归一后 <> '' 命中置 NULL。
    """
    _sub = f"(SELECT ar.engine_uid FROM agent_runtimes ar WHERE ar.id={tbl}.runtime_id)"
    _keep_norm = f"COALESCE({_sub}, '')"
    return (
        f"UPDATE {tbl} SET engine_uid={_sub} "
        f"WHERE runtime_id>0 AND "
        f"COALESCE(NULLIF(engine_uid, ''), '')<>{_keep_norm}"
    )


class RuntimeRefReconciler:
    """引擎行引用对账例程（runtime 域自有表；由 caller 提供本域游标）。

    用法::

        RuntimeRefReconciler(cursor, reason="dedup").reconcile(ids, keep_id)
        RuntimeRefReconciler(cursor, reason="revoke").reconcile(ids, 0)
    """

    # 报告骨架（键序固定，便于日志/断言比对）
    _REPORT_DEFAULTS = {
        "tasks_rebound": 0,
        "tasks_snapshot_kept": 0,
        "pool_rows_deleted": 0,
        "capability_rows_deleted": 0,
        "sessions_rebound": 0,
        "sessions_reset": 0,
        "sessions_uid_rebound": 0,
        "sessions_uid_nulled": 0,
        "chat_sessions_rebound": 0,
        "chat_sessions_reset": 0,
        "chat_sessions_uid_rebound": 0,
        "chat_sessions_uid_nulled": 0,
        "runtime_bindings_deleted": 0,
        "scene_bindings_cleaned": 0,
    }

    def __init__(self, cursor, reason: str = "", emit_warn=None):
        self.cursor = cursor
        self.reason = reason
        self._emit_warn = emit_warn or self._default_warn
        self.report: dict = {}

    # ── 告警 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _default_warn(payload: dict) -> None:
        log.warning(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _warn(self, table: str, error, action: str = "skipped") -> None:
        """记录一次「该步骤未完成」：进报告 + 结构化日志（不静默）。"""
        item = {
            "event": WARN_EVENT,
            "mode": self.report.get("mode", ""),
            "reason": self.report.get("reason", ""),
            "table": table,
            "action": action,
            "error": str(error)[:300],
        }
        self.report.setdefault("errors", []).append(item)
        try:
            self._emit_warn(item)
        except Exception as exc:  # noqa: BLE001  # 告警通道故障不得拖垮对账主流程
            log.warning(f"{WARN_EVENT}: warn sink failed: {exc} (payload={item})")

    # ── 主流程 ────────────────────────────────────────────────────────────
    def reconcile(self, runtime_ids: list, keep_id: int) -> dict:
        c = self.cursor
        ids = [i for i in (runtime_ids or []) if i]
        mode = "rebind" if keep_id else "explicit_cleanup"
        self.report = {
            "mode": mode,
            "reason": self.reason or ("dedup-rebind" if keep_id else "engine-delete"),
            "runtime_ids": ids,
            "errors": [],
            **self._REPORT_DEFAULTS,
        }
        if not ids:
            return self.report
        ph = ",".join("?" for _ in ids)
        _uid_ready = (columns_exist(c, "agent_runtimes", ("engine_uid",))
                      and columns_exist(c, "agent_type_runtime_pool", ("engine_uid",)))
        drop_uids: list = []
        keep_uid = ""
        keep_name = ""
        if _uid_ready:
            try:
                drop_uids = [r[0] for r in c.execute(
                    f"SELECT engine_uid FROM agent_runtimes WHERE id IN ({ph})", ids).fetchall() if r and r[0]]
                if keep_id:
                    _kr = c.execute(
                        "SELECT engine_uid, COALESCE(display_name, name), name FROM agent_runtimes WHERE id=?",
                        (keep_id,)).fetchone()
                    if _kr and _kr[0]:
                        keep_uid = _kr[0]
                        keep_name = _kr[1] or _kr[2] or ""
            except Exception as _e:  # noqa: BLE001  # 降级单轨 + 结构化 warn
                drop_uids = []
                self._warn("agent_runtimes", _e, action="uid-track-skipped")
        else:
            # 旧库未迁移（缺 engine_uid 列）→ runtime_id 单轨；记录以便分辨「降级」与「失败」
            self._warn("agent_runtimes", "engine_uid column unavailable", action="uid-track-skipped")

        self._reconcile_tasks(ids, keep_id, ph, keep_uid, keep_name)
        self._reconcile_pool_and_capabilities(ids, keep_id, ph, drop_uids, keep_uid)
        self._reconcile_sessions(ids, keep_id, ph)
        self._reconcile_binding_table(ids, keep_id, ph)
        if not keep_id:
            self._cleanup_scene_bindings(ids)

        log.info("Issue #646 引用%s对账报告: %s", mode,
                 json.dumps(self.report, ensure_ascii=False))
        return self.report

    # ── 1. agent_tasks ────────────────────────────────────────────────────
    def _reconcile_tasks(self, ids, keep_id, ph, keep_uid, keep_name) -> None:
        c = self.cursor
        try:
            _n = c.execute(f"SELECT COUNT(*) FROM agent_tasks WHERE runtime_id IN ({ph})", ids).fetchone()[0]
            if keep_id:
                c.execute(f"UPDATE agent_tasks SET runtime_id=? WHERE runtime_id IN ({ph})", (keep_id, *ids))
                self.report["tasks_rebound"] = _n
                # QA 复审 R2：改绑后 uid 轨道补齐（「先回填去重（重复行 uid 置 NULL）
                # 再改绑」顺序下引用行 engine_uid 为 NULL，镜像 UPDATE IN(drop_uids)
                # 匹配不到；按保留行 uid/名称补齐，不留 NULL 尾巴）。
                if keep_uid:
                    c.execute(
                        "UPDATE agent_tasks SET engine_uid=?, "
                        "engine_name_snapshot=CASE WHEN engine_name_snapshot IS NULL OR engine_name_snapshot='' "
                        "THEN ? ELSE engine_name_snapshot END "
                        "WHERE runtime_id=? AND (engine_uid IS NULL OR engine_uid='')",
                        (keep_uid, keep_name, keep_id))
            else:
                self.report["tasks_snapshot_kept"] = _n
        except Exception as _e:  # noqa: BLE001  # 记结构化 warn，不静默
            self._warn("agent_tasks", _e, action="step-failed")

    # ── 2. 池/能力绑定 ────────────────────────────────────────────────────
    def _reconcile_pool_and_capabilities(self, ids, keep_id, ph, drop_uids, keep_uid) -> None:
        c = self.cursor
        for tbl, key_col, cnt_key in (("agent_type_runtime_pool", "agent_type", "pool_rows_deleted"),
                                      ("engine_capabilities", "tag", "capability_rows_deleted")):
            try:
                if keep_id:
                    rows = c.execute(
                        f"SELECT id, {key_col} FROM {tbl} WHERE runtime_id IN ({ph})", ids).fetchall()
                    for pid, key_val in rows:
                        keep_row = c.execute(
                            f"SELECT id FROM {tbl} WHERE runtime_id=? AND {key_col}=? LIMIT 1",
                            (keep_id, key_val)).fetchone()
                        if keep_row:
                            c.execute(f"DELETE FROM {tbl} WHERE id=?", (pid,))
                            self.report[cnt_key] += 1
                        else:
                            c.execute(f"UPDATE {tbl} SET runtime_id=? WHERE id=?", (keep_id, pid))
                    # uid 镜像统一指向保留行（旧行/双轨残留行补齐，防 RESTRICT 兜底误伤）
                    if drop_uids:
                        _phu = ",".join("?" for _ in drop_uids)
                        if keep_uid:
                            c.execute(f"UPDATE {tbl} SET engine_uid=? WHERE engine_uid IN ({_phu})",
                                      (keep_uid, *drop_uids))
                        else:
                            c.execute(f"UPDATE {tbl} SET engine_uid=NULL WHERE engine_uid IN ({_phu})",
                                      drop_uids)
                    # QA 复审 R2：改绑到保留行的行 uid 为 NULL 时按保留行 uid 补齐
                    if keep_uid:
                        c.execute(
                            f"UPDATE {tbl} SET engine_uid=? WHERE runtime_id=? "
                            f"AND (engine_uid IS NULL OR engine_uid='')",
                            (keep_uid, keep_id))
                else:
                    if drop_uids:
                        _phu = ",".join("?" for _ in drop_uids)
                        cond = f"runtime_id IN ({ph}) OR engine_uid IN ({_phu})"
                        params = (*ids, *drop_uids)
                    else:
                        cond = f"runtime_id IN ({ph})"
                        params = tuple(ids)
                    self.report[cnt_key] = c.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE {cond}", params).fetchone()[0]
                    c.execute(f"DELETE FROM {tbl} WHERE {cond}", params)
            except Exception as _e:  # noqa: BLE001  # 记结构化 warn，不静默
                self._warn(tbl, _e, action="step-failed")

    # ── 3. 会话引用 ──────────────────────────────────────────────────────
    def _reconcile_sessions(self, ids, keep_id, ph) -> None:
        """改绑到保留行（去重）或置 0（删除）；同步 uid 轨道（Issue #868 T10）。"""
        c = self.cursor
        for _tbl, _prefix in (("devops_agent_session", ""), ("chat_agent_session", "chat_")):
            try:
                _n = c.execute(
                    f"SELECT COUNT(*) FROM {_tbl} WHERE runtime_id IN ({ph})", ids).fetchone()[0]
                if keep_id:
                    c.execute(f"UPDATE {_tbl} SET runtime_id=? WHERE runtime_id IN ({ph})",
                              (keep_id, *ids))
                    self.report[_prefix + "sessions_rebound"] = _n
                else:
                    c.execute(f"UPDATE {_tbl} SET runtime_id=0 WHERE runtime_id IN ({ph})", ids)
                    self.report[_prefix + "sessions_reset"] = _n
                # uid 轨道对账（NULL-safe 条件；孤儿 stale uid 下方置 NULL）
                if columns_exist(c, _tbl, ("engine_uid",)):
                    _mirror = c.execute(session_uid_mirror_sql(_tbl)).rowcount
                    self.report[_prefix + "sessions_uid_rebound"] = _mirror
                    _nulled = c.execute(
                        f"SELECT COUNT(*) FROM {_tbl} WHERE engine_uid IS NOT NULL "
                        f"AND runtime_id NOT IN (SELECT id FROM agent_runtimes)").fetchone()[0]
                    if _nulled:
                        c.execute(
                            f"UPDATE {_tbl} SET engine_uid=NULL "
                            f"WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)")
                    self.report[_prefix + "sessions_uid_nulled"] = _nulled
                else:
                    # 历史库缺 engine_uid 列：uid 轨道跳过（记录，不静默）
                    self._warn(_tbl, "engine_uid column unavailable", action="uid-track-skipped")
            except Exception as _e:  # noqa: BLE001  # 记结构化 warn，不静默
                self._warn(_tbl, _e, action="step-failed")

    # ── 4. 历史绑定表 ─────────────────────────────────────────────────────
    def _reconcile_binding_table(self, ids, keep_id, ph) -> None:
        # 该表归 mgmt：SQL 只在 mgmt public 一份，这里只调原语（跨域写收口）。
        # ph 参数保留在签名里是为了兼容既有调用方，原语内部自己按列表长度拼占位符。
        try:
            from src.domains.mgmt.public import purge_runtime_binding, reassign_runtime_binding
            if keep_id:
                reassign_runtime_binding(ids, keep_id)
            else:
                self.report["runtime_bindings_deleted"] = purge_runtime_binding(ids)
        except Exception as _e:  # noqa: BLE001  # 历史库未建表=预期降级，但仍登记
            self._warn("devops_agent_runtime_binding", _e, action="table-unavailable")

    # ── 5. 场景绑定（仅删除路径）─────────────────────────────────────────
    def _cleanup_scene_bindings(self, ids) -> None:
        try:
            from src.domains.runtime.engine.engine_select import cleanup_scene_engine_bindings
            self.report["scene_bindings_cleaned"] = cleanup_scene_engine_bindings(ids)
        except Exception as _e:  # noqa: BLE001  # 记结构化 warn，不静默
            self._warn("devops_pipeline_scene", _e, action="step-failed")


def reconcile_runtime_references(cursor, runtime_ids: list, keep_id: int,
                                reason: str = "", emit_warn=None) -> dict:
    """函数式入口：引擎行删除前引用改绑/显式清理 + 对账报告。"""
    return RuntimeRefReconciler(cursor, reason=reason, emit_warn=emit_warn).reconcile(
        list(runtime_ids or []), keep_id)


def reconcile_from_owner_domain(runtime_ids: list, keep_id: int, reason: str = "") -> dict | None:
    """跨进程通道：经 **RuntimeService 契约**在目标域执行引用对账。

    返回 None 表示「当前进程就是 runtime 域」或「runtime 未配自有 DSN（单库/复用
    进程库）」——这两种情况下表就在调用方自己的连接可见的库里，调用方应直接用自己的
    游标（同域读写必须留在本地事务，否则会丢调用方事务的回滚能力）。

    通道选择走 GSB provider（Local/Remote 按方法路由，Issue #2479），不在 mgmt 域
    直接 import 外域 public —— 契约是跨域访问的唯一入口（三库四进程不变量 #1）。

    失败补偿：对账是幂等可重放的普通跨域写。执行失败时登记一条 `engine_refs_reconcile`
    意图（`cross_write.submit_deferred`，随调用方事务提交），由排水单元重放并将失败
    原因留在 `grape_t_xwrite_log`；本函数不抛，返回 report（含 errors）给调用方。
    """
    from src.kernel import db_registry

    if db_registry.current_domain() == "runtime" or not db_registry.uses_own_dsn("runtime"):
        return None

    ids = list(runtime_ids or [])
    try:
        from src.kernel.provider import get_runtime_service
        return get_runtime_service().reconcile_engine_references(ids, keep_id, reason=reason)
    except Exception as exc:  # noqa: BLE001  # 目标域不可用：登记意图补偿，不静默
        log.warning(json.dumps({
            "event": WARN_EVENT, "table": "agent_references", "action": "cross-domain-call-failed",
            "runtime_ids": ids, "keep_id": keep_id, "error": str(exc)[:300],
        }, ensure_ascii=False, sort_keys=True))
        _queue_for_drain(ids, keep_id, reason)
        return {"mode": "rebind" if keep_id else "explicit_cleanup",
                "runtime_ids": ids, "errors": [f"cross-domain call failed: {exc}"[:300]]}


def _queue_for_drain(runtime_ids: list, keep_id: int, reason: str) -> None:
    """把失败的对账登记为可重放意图（随调用方事务提交，排水单元执行）。"""
    try:
        from src.kernel import cross_write as xw
        res = xw.submit_deferred(
            "runtime", "engine_refs_reconcile",
            xw.unique_key(f"engine-refs:{keep_id}:{len(runtime_ids)}"),
            {"runtime_ids": list(runtime_ids), "keep_id": int(keep_id), "reason": reason})
        log.info(json.dumps({"event": "engine_refs_reconcile_queued",
                             "runtime_ids": runtime_ids, "keep_id": keep_id,
                             "result": res.get("status") if isinstance(res, dict) else res},
                            ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001  # 意图登记失败只能记日志（事务可能已回滚）
        log.warning(json.dumps({"event": "engine_refs_reconcile_queue_failed",
                                "error": str(exc)[:300]}, ensure_ascii=False))


def reconcile_for_caller(caller_cursor, runtime_ids: list, keep_id: int, reason: str = "") -> dict:
    """统一入口：按进程域自动选通道（跨域 → 契约；同域/单库 → 调用方游标）。

    mgmt/user/db.py 的接线只调这一个函数（参数整形 + 一行委托）。
    """
    ids = list(runtime_ids or [])
    via_owner = reconcile_from_owner_domain(ids, keep_id, reason=reason)
    if via_owner is not None:
        return via_owner
    return reconcile_runtime_references(caller_cursor, ids, keep_id, reason=reason)