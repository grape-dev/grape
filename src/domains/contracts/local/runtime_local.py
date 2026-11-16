"""RuntimeService LocalProvider — 同实例跨 schema SQL 实现（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.4.

同实例模式：直接查 grape_runtime schema（SQLite 用 rt_ 前缀）。
表名通过 t_runtime() 映射，在 GRAPE_SCHEMA_MODE 未启用时返回裸表名（存量零回归）。
"""
from __future__ import annotations

import json
from typing import Any

from src.kernel import db_registry, session_manager
from src.kernel.db import t_runtime
from src.domains.contracts.runtime_service import RuntimeService


class RuntimeServiceLocal(RuntimeService):
    """同实例模式：直接查 grape_runtime schema（SQLite 用 rt_ 前缀）。"""

    @property
    def _own_dsn(self) -> bool:
        """runtime 是否走自己的 DSN（lib 模式）。"""
        return db_registry.uses_own_dsn("runtime")

    @property
    def _db(self):
        return db_registry.domain_db("runtime") or session_manager.db

    @property
    def _lock(self):
        return db_registry.domain_lock("runtime") if self._own_dsn else session_manager.db_lock

    def _t(self, name: str) -> str:
        """表名：自有 DSN → 裸名；跨 schema 复用进程库 → t_runtime(name)。"""
        return name if self._own_dsn else t_runtime(name)

    def get_events(self, repo_key: str, limit: int = 50) -> list:
        """A 用户面事件列表——返回 events 行列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT seq, repo_key, event_type, source, issue_id, title, "
                f"summary, actor, agent_type, status, exit_code, duration, "
                f"created_at, started_at, completed_at "
                f"FROM {self._t('events')} WHERE repo_key=? "
                f"ORDER BY seq DESC LIMIT ?",
                (repo_key, limit),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_event_detail(self, seq: int) -> dict:
        """A 事件详情。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT * FROM {self._t('events')} WHERE seq=?",
                (seq,),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)

    def get_tasks(self, repo_key: str, limit: int = 50) -> list:
        """A 任务列表——返回 agent_tasks 行列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, agent_session_id, runtime_id, event_seq, issue_id, "
                f"repo_key, agent_type, skill, status, exit_code, "
                f"started_at, completed_at, created_at "
                f"FROM {self._t('agent_tasks')} WHERE repo_key=? "
                f"ORDER BY created_at DESC LIMIT ?",
                (repo_key, limit),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_task_messages(self, task_id: str, after_id: int = 0) -> list:
        """A SSE 流——返回 task_messages 增量行列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, task_id, seq, type, tool, content, "
                f"input_json, output, created_at "
                f"FROM {self._t('task_messages')} "
                f"WHERE task_id=? AND id>? ORDER BY seq ASC",
                (task_id, after_id),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_runtimes(self) -> list:
        """A 引擎列表——返回 agent_runtimes 行列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, credential_id, name, display_name, provider, "
                f"installed, cli_path, version, healthy, status, "
                f"last_seen_at, scope, owner_id "
                f"FROM {self._t('agent_runtimes')} ORDER BY id"
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_issue_state(self, repo_key: str, issue_number: int) -> dict:
        """A Issue 阶段状态——返回 issue_state 当前行。"""
        issue_id = f"#{issue_number}" if not str(issue_number).startswith("#") else str(issue_number)
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, repo_key, issue_id, stage, owner_agent, "
                f"assignee, a2a_seq, scene_id, updated_at, created_at "
                f"FROM {self._t('issue_state')} "
                f"WHERE repo_key=? AND issue_id=?",
                (repo_key, issue_id),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)

    def list_credential_rows(self) -> list:
        from src.domains.runtime.public import list_credential_rows as _f
        return [list(r) for r in _f()]

    def list_engine_tags(self, runtime_id: int, enabled_only: bool = True) -> list:
        from src.domains.runtime.public import list_engine_tags as _f
        return list(_f(int(runtime_id), bool(enabled_only)))

    def get_agent_session_row(self, repo_keys: list, agent_type: str, token_field: str) -> list:
        from src.domains.runtime.public import agent_session_row as _f
        row = _f(repo_keys, agent_type, token_field)
        return list(row) if row else []

    def get_event_status_counts(self, repo_keys: list, agent_type: str) -> list:
        from src.domains.runtime.public import event_status_counts as _f
        return [list(r) for r in _f(repo_keys, agent_type)]

    def search_events(self, repo_keys: list, agent_type: str, keyword: str = "",
                      page: int = 1, page_size: int = 10) -> dict:
        from src.domains.runtime.public import events_search as _f
        total, rows = _f(repo_keys, agent_type, keyword, page, page_size)
        return {"total": int(total), "rows": [list(r) for r in rows]}

    def get_credential_health_rows(self) -> list:
        """复用 runtime public 的具名查询（不复制 SQL）。"""
        from src.domains.runtime.public import credential_health_rows_any
        return [list(r) for r in credential_health_rows_any()]

    def get_inflight_task_count(self, repo_keys: list) -> int:
        from src.domains.runtime.public import inflight_task_count_any
        return int(inflight_task_count_any(repo_keys))

    def get_issue_card(self, repo_key: str, issue_number: int) -> dict:
        """A Issue 卡片状态——复用 runtime public 的 issue_card_row 具名查询。

        不自己拼 SQL：字段顺序与过滤条件都在 owner 域里定义，这里只做形状归一，
        保证 Local 与 Remote 两条路径返回同一结构。
        """
        from src.domains.runtime.public import read_one_any
        _num = int(str(issue_number).lstrip("#"))
        row = read_one_any("issue_card_row", (repo_key, _num))
        if not row:
            return {}
        return {"id": None, "stage": row[0], "owner_agent": row[1],
                "a2a_seq": row[2], "updated_at": row[3], "assignee": row[4] or "",
                "scene_id": row[5] or "", "scene_user_id": row[6] or 0,
                "parent_issue_id": row[7] or "", "is_subtask_mode": row[8] or 0,
                "allow_scene_override": row[9] or 0}

    def update_issue_state(self, repo_key: str, issue_number: int, patch: dict) -> Any:
        """A 用户操作推进 Issue 状态。

        Args:
            patch: {"stage": "coding", "owner_agent": "coding", ...}
        """
        issue_id = f"#{issue_number}" if not str(issue_number).startswith("#") else str(issue_number)
        with self._lock:
            c = self._db.cursor()
            set_clauses = []
            params = []
            for k, v in patch.items():
                if k in ("stage", "owner_agent", "assignee", "a2a_seq", "scene_id"):
                    set_clauses.append(f"{k}=?")
                    params.append(v)
            if not set_clauses:
                return {"ok": False, "error": "no valid fields in patch"}
            set_clauses.append("updated_at=datetime('now')")
            params.extend([repo_key, issue_id])
            c.execute(
                f"UPDATE {self._t('issue_state')} "
                f"SET {', '.join(set_clauses)} "
                f"WHERE repo_key=? AND issue_id=?",
                params,
            )
            self._db.commit()
            return {"ok": True, "repo_key": repo_key, "issue_id": issue_id}

    def get_chat_conversations(self, repo_key: str, limit: int = 50) -> list:
        """A 用户面聊天对话列表（v2.2：chat 表归运行域，Mgmt 经本接口读）。"""
        with self._lock:
            c = self._db.cursor()
            if repo_key:
                c.execute(
                    f"SELECT id, agent_session_id, user_id, backend_session_id, "
                    f"repo_key, im_channel_id, title, status, created_at, updated_at "
                    f"FROM {self._t('chat_conversations')} "
                    f"WHERE repo_key=? AND status!='deleted' "
                    f"ORDER BY updated_at DESC LIMIT ?",
                    (repo_key, limit),
                )
            else:
                c.execute(
                    f"SELECT id, agent_session_id, user_id, backend_session_id, "
                    f"repo_key, im_channel_id, title, status, created_at, updated_at "
                    f"FROM {self._t('chat_conversations')} "
                    f"WHERE status!='deleted' "
                    f"ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_chat_messages(self, conv_id: str, limit: int = 100, before: int = 0) -> list:
        """A 用户面聊天消息分页（Mgmt SSE 流/历史展示）。"""
        with self._lock:
            c = self._db.cursor()
            if before > 0:
                c.execute(
                    f"SELECT id, conversation_id, role, content, created_at, "
                    f"task_id, generate_status, deleted "
                    f"FROM {self._t('chat_messages')} "
                    f"WHERE conversation_id=? AND id<? "
                    f"ORDER BY id DESC LIMIT ?",
                    (conv_id, before, limit),
                )
            else:
                c.execute(
                    f"SELECT id, conversation_id, role, content, created_at, "
                    f"task_id, generate_status, deleted "
                    f"FROM {self._t('chat_messages')} "
                    f"WHERE conversation_id=? "
                    f"ORDER BY id DESC LIMIT ?",
                    (conv_id, limit),
                )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]


    # ---- P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12） ----

    @staticmethod
    def _skill_row(row, cols) -> dict:
        d = dict(zip(cols, row)) if cols else dict(row)
        d["name"] = d.get("skill_name") or ""
        d["summary"] = d.get("description") or ""
        return d

    def list_skills(self, repo_key: str = "", q: str = "", limit: int = 50,
                    offset: int = 0) -> list:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql = (f"SELECT id, skill_name, display_name, description, agent_type, scope, "
               f"owner_id, category_id, is_active, is_featured, like_count, updated_at, org_id "
               f"FROM {self._t('skill')} WHERE is_active=1")
        args: list = []
        if q:
            sql += " AND (skill_name LIKE ? OR display_name LIKE ? OR description LIKE ?)"
            args += [f"%{q}%"] * 3
        if repo_key:
            sql += " AND (scope='official' OR owner_id=? OR scope=?)"
            args += [repo_key, repo_key]
        sql += " ORDER BY is_featured DESC, id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            c = self._db.cursor()
            c.execute(sql, tuple(args))
            rows = c.fetchall()
            cols = [d[0] for d in c.description] if c.description else []
            return [self._skill_row(r, cols) for r in rows]

    def get_skill_detail(self, skill_id: int) -> dict:
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, skill_name, display_name, description, agent_type, scope, "
                f"owner_id, category_id, is_featured, like_count, updated_at, org_id, file_path "
                f"FROM {self._t('skill')} WHERE id=? AND is_active=1", (int(skill_id),))
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
        out = self._skill_row(row, cols)
        out["content_url"] = ""
        key = out.pop("file_path", "") or ""
        if key:
            try:
                from src.domains.runtime.storage import get_storage

                storage = get_storage()
                if storage is not None:
                    out["content_url"] = storage.temporary_url(key, 300)
            except Exception:  # noqa: BLE001 - 存储不可用不等于接口失败
                out["content_url"] = ""
        return out

    def list_engine_capabilities(self, limit: int = 500) -> list:
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, runtime_id, tag, max_concurrent, priority, enabled, updated_at "
                f"FROM {self._t('engine_capabilities')} "
                f"ORDER BY runtime_id, priority DESC, tag LIMIT ?",
                (max(1, min(int(limit), 500)),))
            rows = c.fetchall()
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) if cols else dict(r) for r in rows]

    def get_runtime_credential_meta(self, runtime_id: int) -> dict:
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, name, device_name, scope, owner_id, owner_name, repo_key, status, "
                f"last_seen_at, last_ip, spec, org_id, cpu_percent, memory_percent, disk_percent, "
                f"load_avg FROM {self._t('runtime_credentials')} WHERE id=?", (int(runtime_id),))
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)

    _META_NAMESPACES = ("a2a-redo", "bot_token_health", "allow_signup", "enable_aligned",
                        "admin_initialized", "global_bot_usernames", "global_bot_token_status",
                        "skill_sync", "scene_sync")

    def get_meta_namespace(self, namespace: str) -> dict:
        ns = (namespace or "").strip()
        if ns not in self._META_NAMESPACES:
            raise ValueError(f"namespace '{ns}' 未登记（拒绝读取）")
        with self._lock:
            c = self._db.cursor()
            c.execute(f"SELECT `key`, `value` FROM {self._t('meta')} "
                      f"WHERE `key`=? OR `key` LIKE ? ORDER BY `key` LIMIT 500",
                      (ns, ns + ":%"))
            return {r[0]: r[1] for r in c.fetchall()}

    def get_tasks_batch(self, task_ids: list) -> list:
        raw = [t for t in (task_ids or [])
               if isinstance(t, int) or str(t).strip().lstrip("-").isdigit()]
        ids = [int(t) for t in raw][:200]
        if not ids:
            return []
        marks = ",".join(["?"] * len(ids))
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, repo_key, agent_type, skill, status, exit_code, runtime_id, "
                f"event_seq, started_at, completed_at, created_at "
                f"FROM {self._t('agent_tasks')} WHERE id IN ({marks})", tuple(ids))
            rows = c.fetchall()
            cols = [d[0] for d in c.description] if c.description else []
        out = [dict(zip(cols, r)) if cols else dict(r) for r in rows]
        order = {tid: i for i, tid in enumerate(ids)}
        out.sort(key=lambda r: order.get(int(r["id"]), 10 ** 9) if r.get("id") is not None else 10 ** 9)
        return out

    def get_task_stats(self, repo_key: str = "", since: str = "") -> dict:
        sql = (f"SELECT status, COUNT(*) FROM {self._t('agent_tasks')} WHERE 1=1")
        args: list = []
        if repo_key:
            sql += " AND repo_key=?"
            args.append(repo_key)
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        sql += " GROUP BY status"
        with self._lock:
            c = self._db.cursor()
            c.execute(sql, tuple(args))
            by_status = {r[0]: int(r[1]) for r in c.fetchall()}
        total = sum(by_status.values())
        done = sum(n for s, n in by_status.items() if s in ("completed", "success"))
        return {"by_status": by_status,
                "aggregate": {"total": total, "done": done,
                              "failed": by_status.get("failed", 0),
                              "completion_rate": round(done / total, 4) if total else 0.0}}
