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
