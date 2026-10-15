"""Issue #287: 定时任务（autopilot）入队前预解析 user scope skill_id 测试。

验证点：
  1. user scope skill（owner_id=created_by）→ _execute_autopilot_remote 传给
     enqueue_task 的 skill_id 非零、user_id 仍为 0（凭据注入行为零变化）
  2. 不可解析 skill → skill_id=0 + 任务照常入队（降级不阻断）
  3. repo scope / official 解析路径回归不受影响（resolve_skill_id 既有行为）
"""
import os
import uuid

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest
from unittest.mock import patch

from src.user import db as user_db
from src.auth import grape_account_db
from src.runtime import scheduler
from src.session_manager import db, db_lock


def _mk_user(prefix="ap287"):
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    u = grape_account_db.create_grape_account(uname, email=None, password="pw123")
    if not u:
        u = grape_account_db.get_grape_account_by_username(uname)
    return u, uname


def _cleanup(uname):
    u = grape_account_db.get_grape_account_by_username(uname)
    if u:
        grape_account_db.soft_delete_account(u["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


def _mk_user_skill(uid, sname):
    assert user_db.create_skill(uid, sname, "issue")
    skills = [s for s in user_db.list_skills(uid) if s["skill_name"] == sname]
    assert skills, f"user skill {sname} not created"
    return skills[0]["id"]


class TestAutopilotSkillPreResolve:
    def _run_remote(self, task, skill_name):
        """驱动 _execute_autopilot_remote，捕获 enqueue_task 收到的 kwargs。"""
        captured = {}

        def _fake_enqueue(**kwargs):
            captured.update(kwargs)
            return "t_fake287"

        statuses = []

        with patch("src.runtime.service.enqueue_task", side_effect=_fake_enqueue),              patch("src.api.session_store.get_or_create_agent_session",
                   return_value={"id": "sess-287"}),              patch("src.prompt_builder._resolve_server_url", return_value="http://srv"),              patch.object(scheduler, "_update_execution_result",
                          side_effect=lambda *a, **k: statuses.append(a)):
            scheduler._execute_autopilot_remote(
                repo_key="gitcode:org/test287", task=task, exec_id=998287,
                agent_type="issue", skill_name=skill_name, engine_id=1,
                backend="", inst_name="opencode-1", ws="/tmp/ws287",
                prompt="p", repo_token="", agent_display_name="bot",
                env_extra={}, repository="org/test287", evt={})
        return captured, statuses

    def test_user_scope_skill_preresolved(self):
        """user scope skill 按 created_by 预解析命中；user_id 保持 0。"""
        u, uname = _mk_user()
        sname = f"usk_{uuid.uuid4().hex[:6]}"
        try:
            sid = _mk_user_skill(u["id"], sname)
            task = {"created_by": u["id"], "engine_runtime_ids": ""}
            captured, statuses = self._run_remote(task, sname)
            assert captured.get("skill_id") == sid,                 f"应传预解析 skill_id={sid}, got {captured.get('skill_id')}"
            assert captured.get("user_id") == 0, "user_id 必须保持 0（Issue #287 边界）"
            assert captured.get("skill") == sname
            assert "dispatched" in [s[1] for s in statuses], statuses
        finally:
            _cleanup(uname)

    def test_unresolvable_skill_degrades_without_blocking(self):
        """解析不中 → skill_id=0 + warning，任务仍入队不阻断。"""
        u, uname = _mk_user()
        try:
            task = {"created_by": u["id"], "engine_runtime_ids": ""}
            captured, statuses = self._run_remote(task, "no_such_skill_287xyz")
            assert captured.get("skill_id") == 0
            assert captured.get("user_id") == 0
            assert "dispatched" in [s[1] for s in statuses], "降级不得阻断入队"
        finally:
            _cleanup(uname)

    def test_missing_created_by_still_enqueues(self):
        """历史任务 created_by 为空 → 与旧行为一致（skill_id=0）照常入队。"""
        captured, statuses = self._run_remote(
            {"created_by": 0, "engine_runtime_ids": ""}, "some_skill_287")
        assert captured.get("skill_id") == 0
        assert "dispatched" in [s[1] for s in statuses]

    def test_repo_and_official_scope_regression(self):
        """回归：repo scope 命中路径不受 created_by 传参影响。"""
        u, uname = _mk_user()
        sname = f"rsk_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test287"
        try:
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO skill (skill_name, agent_type, scope, owner_id, is_active) "
                    "VALUES (?,?, 'repo', ?, 1)", (sname, "issue", rk))
                db.commit()
            sid = user_db.resolve_skill_id(sname, repo_key=rk, user_id=u["id"])
            assert sid and sid > 0, "repo scope 应命中"
            task = {"created_by": u["id"], "engine_runtime_ids": ""}
            captured, _ = self._run_remote(task, sname)
            assert captured.get("skill_id") == sid
            assert captured.get("user_id") == 0
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
                db.commit()
            _cleanup(uname)