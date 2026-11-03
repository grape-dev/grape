"""MgmtService LocalProvider — 同实例跨 schema SQL 实现（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.3.

同实例模式：直接查 grape_mgmt schema（SQLite 用 mgmt_ 前缀）。
表名通过 t_mgmt() 映射，在 GRAPE_SCHEMA_MODE 未启用时返回裸表名（存量零回归）。
"""
from __future__ import annotations

import json
from typing import Any

from src.kernel.db import t_mgmt
from src.kernel import session_manager
from src.domains.contracts.mgmt_service import MgmtService


class MgmtServiceLocal(MgmtService):
    """同实例模式：直接查 grape_mgmt schema（SQLite 用 mgmt_ 前缀）。"""

    @property
    def _db(self):
        return session_manager.db

    @property
    def _lock(self):
        return session_manager.db_lock

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        """B 校验 webhook 时调用——返回 webhook_token, bot_username, org_id。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT repo_key, gitcode_webhook_token AS webhook_token, "
                f"bot_username, org_id "
                f"FROM {t_mgmt('managed_repos')} WHERE repo_key=?",
                (repo_key,),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        """B 路由判定时调用——返回 runtime_id, skill, skill_id。

        devops_agent_session 无 runtime_id/skill_id 列，skill 从 skill 字段取，
        runtime_id 由 agent_type_runtime_pool 查询补全（0=未绑定）。
        """
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, repo_key, agent_type, skill, status "
                f"FROM {t_mgmt('devops_agent_session')} "
                f"WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            data = dict(zip(cols, row)) if cols else dict(row)
            # 补全 runtime_id：从 agent_type_runtime_pool 查询绑定的引擎
            try:
                c.execute(
                    f"SELECT runtime_id FROM {t_mgmt('agent_type_runtime_pool')} "
                    f"WHERE agent_type=? AND enabled=1 "
                    f"ORDER BY priority ASC, weight DESC LIMIT 1",
                    (agent_type,),
                )
                pool_row = c.fetchone()
                data["runtime_id"] = pool_row[0] if pool_row else 0
            except Exception:
                data["runtime_id"] = 0
            data["skill_id"] = data.get("skill", "")
            return data

    def get_scene_config(self, repo_key: str) -> list:
        """B 读取场景节点——返回 scene_id, nodes_json, name, display_name 列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT scene_id, nodes_json, name "
                f"FROM {t_mgmt('devops_pipeline_scene')} WHERE repo_key=? "
                f"AND is_active=1",
                (repo_key,),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        """C 引擎选择时调用——返回场景绑定的引擎 ID 列表。

        场景→引擎绑定通过 agent_type_runtime_pool 间接关联（按 agent_type 查池）。
        """
        with self._lock:
            c = self._db.cursor()
            # 读场景 nodes_json 获取 agent_type 列表
            c.execute(
                f"SELECT nodes_json FROM {t_mgmt('devops_pipeline_scene')} "
                f"WHERE scene_id=?",
                (scene_id,),
            )
            row = c.fetchone()
            if not row:
                return []
            try:
                nodes = json.loads(row[0]) if row[0] else []
            except (json.JSONDecodeError, TypeError):
                return []
            # 提取 nodes 中的 agent_type
            agent_types = set()
            for node in nodes:
                tag = (node.get("tag") or node.get("skill") or "").lower()
                if tag:
                    agent_types.add(tag)
            if not agent_types:
                return []
            # 查池绑定
            placeholders = ",".join("?" * len(agent_types))
            c.execute(
                f"SELECT DISTINCT runtime_id FROM {t_mgmt('agent_type_runtime_pool')} "
                f"WHERE agent_type IN ({placeholders}) AND enabled=1",
                list(agent_types),
            )
            return [r[0] for r in c.fetchall()]

    def verify_session_token(self, token_hash: str) -> dict:
        """C Worker 鉴权时调用——返回 credential_id, scope, owner_id。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, session_key, repo_key, agent_type, user_id, expires_at "
                f"FROM {t_mgmt('session_api_tokens')} "
                f"WHERE token_sha256=?",
                (token_hash,),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            data = dict(zip(cols, row)) if cols else dict(row)
            data["credential_id"] = data.get("id")
            data["scope"] = "session"
            data["owner_id"] = data.get("user_id", 0)
            return data

    def get_bot_config(self, repo_key: str) -> dict:
        """B/C 读取 Bot 配置——bot_token, bot_username。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT bot_gitcode_token AS bot_token, bot_username, "
                f"gitcode_webhook_token AS webhook_token "
                f"FROM {t_mgmt('managed_repos')} WHERE repo_key=?",
                (repo_key,),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)

    def get_skill(self, skill_id: str) -> Any:
        """技能加载——返回技能定义。"""
        with self._lock:
            c = self._db.cursor()
            try:
                skill_id_int = int(skill_id)
            except (ValueError, TypeError):
                skill_id_int = 0
            if not skill_id_int:
                # 按名称查
                c.execute(
                    f"SELECT * FROM {t_mgmt('skill')} WHERE skill_name=? LIMIT 1",
                    (skill_id,),
                )
            else:
                c.execute(
                    f"SELECT * FROM {t_mgmt('skill')} WHERE id=? LIMIT 1",
                    (skill_id_int,),
                )
            row = c.fetchone()
            if not row:
                return None
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)
