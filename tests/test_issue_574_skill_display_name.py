"""Issue #574: 事件信息中 Skill 展示改为场景选择时的显示名称。

覆盖：
- GET /api/events/detail/{seq} 返回 skill_display_name（skill 表 display_name，条件与场景选择处一致）
- skill 表无记录 / display_name 为空 → skill_display_name 为空串（前端回退 Skill ID）
- GET /api/autopilot/tasks/{id} 与 GET /api/autopilot/tasks 返回 display_name
- events.js / autopilot.js 源码优先使用 display_name 回退 skill_name（含 JS 语法校验）
"""
import os
import subprocess

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.session_manager import db, db_lock
from src.auth import grape_account_db
from src.api.jwt_utils import create_access_token

ROOT = os.path.join(os.path.dirname(__file__), "..")
RK = "gitcode:org/i574"


def _seed_skill(skill_name, display_name="", scope="official", is_active=1):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (skill_name,))
        c.execute(
            "INSERT INTO skill (skill_name, display_name, scope, is_active) VALUES (?,?,?,?)",
            (skill_name, display_name, scope, is_active))
        db.commit()


def _clean_skill(skill_name):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name=?", (skill_name,))
        db.commit()


def _seed_event(seq, skill):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=? AND seq=?", (RK, seq))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
            "agent_logs, result, created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
            (seq, RK, "issue_opened", "webhook", "#574", "t", "s", "", "{}",
             "vod", "opencode", skill, "completed", 0, 1.5,
             "[]", "测试结论"))
        db.commit()


def _jwt(user_id, username):
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _make_user(name="i574_user"):
    uid = grape_account_db.create_grape_account(name, email=None, password="pwd")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username(name)
    return uid


def _detail_client(seq):
    uid = _make_user()
    token = _jwt(uid["id"], uid["username"])
    from src.api.main import app
    with patch("src.api.deps.check_repo_access", return_value=None):
        return TestClient(app).get(
            f"/api/events/detail/{seq}",
            headers={"Authorization": "Bearer " + token})


def _ap_client():
    uid = _make_user()
    token = _jwt(uid["id"], uid["username"])
    # 用户需为仓库托管成员（list_tasks 按 managed_repos 过滤）
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (RK,))
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                  (RK, "org", "i574", "org/i574"))
        c.execute("INSERT INTO user_repos (repo_key, user_id, is_join, is_admin) VALUES (?,?,1,1)",
                  (RK, uid["id"]))
        db.commit()
    from src.api.main import app
    return TestClient(app, headers={"Authorization": "Bearer " + token})


def _seed_task(task_id, skill):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO autopilot_tasks (id, name, repo_key, agent_type, instruction, skill_name, cron, max_retry, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (task_id, "i574_task", RK, "issue", "指令", skill, "0 9 * * *", 3, 1))
        db.commit()


# ── 事件详情 skill_display_name ──

class TestEventDetailSkillDisplayName:
    def test_returns_skill_display_name_from_skill_table(self):
        _seed_skill("vod-cli-crossrepo", display_name="开发者声音分析（CLI版）")
        _seed_event(57401, "vod-cli-crossrepo")
        try:
            r = _detail_client(57401)
            assert r.status_code == 200
            ev = r.json()
            assert ev["skill"] == "vod-cli-crossrepo"
            assert ev["skill_display_name"] == "开发者声音分析（CLI版）"
        finally:
            _clean_skill("vod-cli-crossrepo")

    def test_missing_skill_row_falls_back_to_empty(self):
        _clean_skill("vod-cli-unknown574")
        _seed_event(57402, "vod-cli-unknown574")
        r = _detail_client(57402)
        assert r.status_code == 200
        ev = r.json()
        assert ev["skill"] == "vod-cli-unknown574"
        assert ev["skill_display_name"] == ""

    def test_empty_display_name_falls_back_to_empty(self):
        _seed_skill("coding-cli", display_name="")
        _seed_event(57403, "coding-cli")
        try:
            r = _detail_client(57403)
            assert r.status_code == 200
            ev = r.json()
            assert ev["skill"] == "coding-cli"
            assert ev["skill_display_name"] == ""
        finally:
            _clean_skill("coding-cli")

    def test_inactive_skill_not_used(self):
        _seed_skill("qa-cli", display_name="QA 审查（CLI版）", is_active=0)
        _seed_event(57404, "qa-cli")
        try:
            r = _detail_client(57404)
            assert r.status_code == 200
            ev = r.json()
            assert ev["skill"] == "qa-cli"
            assert ev["skill_display_name"] == ""
        finally:
            _clean_skill("qa-cli")


# ── Autopilot 任务 display_name ──

class TestAutopilotTaskDisplayName:
    def test_task_detail_returns_display_name(self):
        _seed_skill("autopilot-skill-i574", display_name="定时巡检技能")
        _seed_task(57411, "autopilot-skill-i574")
        try:
            client = _ap_client()
            with patch("src.api.routes.autopilot.check_repo_access", return_value=None):
                r = client.get("/api/autopilot/tasks/57411")
            assert r.status_code == 200
            t = r.json()
            assert t["skill_name"] == "autopilot-skill-i574"
            assert t["display_name"] == "定时巡检技能"
        finally:
            _clean_skill("autopilot-skill-i574")
            with db_lock:
                db.cursor().execute("DELETE FROM autopilot_tasks WHERE id=57411")
                db.commit()

    def test_task_list_returns_display_name(self):
        _seed_skill("autopilot-skill-i574b", display_name="定时巡检技能B")
        _seed_task(57412, "autopilot-skill-i574b")
        try:
            client = _ap_client()
            with patch("src.api.routes.autopilot.check_repo_access", return_value=None):
                r = client.get("/api/autopilot/tasks?repo_key=" + RK)
            assert r.status_code == 200
            tasks = r.json().get("tasks", [])
            t = next((x for x in tasks if x["id"] == 57412), None)
            assert t is not None
            assert t["skill_name"] == "autopilot-skill-i574b"
            assert t["display_name"] == "定时巡检技能B"
        finally:
            _clean_skill("autopilot-skill-i574b")
            with db_lock:
                db.cursor().execute("DELETE FROM autopilot_tasks WHERE id=57412")
                db.commit()

    def test_task_unknown_skill_display_name_empty(self):
        _clean_skill("autopilot-unknown-i574")
        _seed_task(57413, "autopilot-unknown-i574")
        try:
            client = _ap_client()
            with patch("src.api.routes.autopilot.check_repo_access", return_value=None):
                r = client.get("/api/autopilot/tasks/57413")
            assert r.status_code == 200
            t = r.json()
            assert t["skill_name"] == "autopilot-unknown-i574"
            assert t["display_name"] == ""
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM autopilot_tasks WHERE id=57413")
                db.commit()


# ── 前端源码断言 ──

BASE_SRC = os.path.join(ROOT, "src", "api", "static", "js", "pages")


@pytest.fixture(scope="module")
def events_js():
    with open(os.path.join(BASE_SRC, "events.js"), "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def autopilot_js():
    with open(os.path.join(BASE_SRC, "autopilot.js"), "r", encoding="utf-8") as f:
        return f.read()


class TestFrontendSource:
    def test_events_js_prioritizes_skill_display_name(self, events_js):
        assert "data.skill_display_name || data.skill" in events_js, (
            "events.js Skill 行应优先取 data.skill_display_name，回退 data.skill"
        )

    def test_autopilot_js_uses_display_name_in_detail_card(self, autopilot_js):
        assert "t.display_name || t.skill_name" in autopilot_js, (
            "autopilot.js 详情页卡片应优先取 display_name，回退 skill_name"
        )

    def test_autopilot_js_uses_display_name_in_refresh_card(self, autopilot_js):
        assert "task.display_name || task.skill_name" in autopilot_js, (
            "autopilot.js 编辑刷新卡片应优先取 display_name，回退 skill_name"
        )

    def test_js_syntax_valid(self):
        for name in ("events.js", "autopilot.js"):
            path = os.path.join(BASE_SRC, name)
            result = subprocess.run(
                ["node", "-e",
                 "new Function(require('fs').readFileSync(process.argv[1],'utf8')); console.log('OK');",
                 path],
                capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, f"{name} 语法错误: {result.stderr}"
            assert "OK" in result.stdout, f"{name} 语法验证未通过: {result.stderr}"