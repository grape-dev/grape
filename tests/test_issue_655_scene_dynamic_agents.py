"""Issue #655: pending-review / awaiting-input agent_type 白名单放宽为场景动态校验。

自定义流水线节点角色（如「场景提取」scene-extract）经会话 tk_ 调用官方审核点接口：
- Issue 绑定自定义场景 → 场景 nodes[].id 集合动态放行（pending-review / awaiting-input 200）
- 场景未绑定（scene_id 空/default）或解析失败 → 回退原白名单（自定义 agent 403）
- 原 vod/issue/coding/qa/sre 角色行为不变（向后兼容）
"""
import os
import sys
import uuid
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import issues as issues_mod
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.auth import grape_account_db

client = TestClient(app)

_REPO_KEY = "gitcode:org/test655"
_REPO = "org/test655"
_SCENE_NODE = "scene-extract"  # 自定义节点（场景提取）

_uid = None
_BOT_TOKEN = "bot_655_test_token"


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


def _session_headers(repo_key=_REPO_KEY, agent_type="scene-extract"):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i655-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i655_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='i655_eric'")
        db.commit()
    _uid = grape_account_db.create_grape_account("i655_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("i655_user")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (_uid["id"], "i655_eric", "ou_655", "Eric"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                  "VALUES (?,?,?,?,?,?)", (_REPO_KEY, "org", "test655", "org/test655", 1, _BOT_TOKEN))
        db.commit()
    user_db.mark_user_repo_join(_uid["id"], _REPO_KEY, True, True)


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i655_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='i655_eric'")
        try:
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (_REPO_KEY,))
        except Exception:
            pass
        c.execute("DELETE FROM issue WHERE repo_key=?", (_REPO_KEY,))
        db.commit()


def _seed_scene():
    from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache
    invalidate_scene_cache()
    scene_id = f"i655-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (_REPO_KEY,))
        db.commit()
    invalidate_scene_cache()
    create_custom_scene(
        scene_id=scene_id, name="自定义流水线场景", description="d", repo_key=_REPO_KEY,
        nodes=[
            {"id": "vod", "skill": "vod", "mode": "emit", "next": _SCENE_NODE},
            {"id": _SCENE_NODE, "skill": "custom", "mode": "emit", "next": "issue"},
            {"id": "issue", "skill": "issue", "mode": "emit", "next": "done"},
        ])
    return scene_id


def _seed_issue(status="processing", issue_number=61, scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (_REPO_KEY, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, stage, scene_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_uid["id"], "i655_eric", _REPO_KEY, issue_number, "#655 需求", "d", status, "vod", scene_id))
        card_id = c.lastrowid
        db.commit()
        return card_id


def _status(issue_number):
    with db_lock:
        return db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
            (_REPO_KEY, issue_number)).fetchone()[0]


class TestSceneDynamicAllowlist:
    def test_custom_agent_pending_review(self):
        scene_id = _seed_scene()
        cid = _seed_issue(issue_number=61, scene_id=scene_id)
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(), json={"review_points": "阶段产物请审核"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["status"] == "pending_review" and data["changed"] is True
        assert _status(61) == "pending_review"

    def test_custom_agent_awaiting_input(self):
        scene_id = _seed_scene()
        cid = _seed_issue(issue_number=62, scene_id=scene_id)
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/awaiting-input",
                            headers=_session_headers(), json={"questions": "请补充验收标准"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "awaiting_input" and data["changed"] is True
        assert _status(62) == "awaiting_input"

    def test_agent_not_in_this_scene_403(self):
        scene_id = _seed_scene()
        cid = _seed_issue(issue_number=63, scene_id=scene_id)
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(agent_type="other-agent"), json={"review_points": "x"})
        assert r.status_code == 403

    def test_original_roles_still_allowed_with_scene(self):
        scene_id = _seed_scene()
        for agent in ("vod", "issue", "coding", "qa", "sre"):
            cid = _seed_issue(issue_number=64, scene_id=scene_id)
            with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post(f"/api/issues/{cid}/pending-review",
                                headers=_session_headers(agent_type=agent), json={"review_points": "x"})
            assert r.status_code == 200, (agent, r.text)


class TestFallbackWhitelist:
    def test_unbound_scene_custom_agent_403(self):
        cid = _seed_issue(issue_number=65, scene_id="")
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(), json={"review_points": "x"})
        assert r.status_code == 403
        r = client.post(f"/api/issues/{cid}/awaiting-input",
                        headers=_session_headers(), json={"questions": "q"})
        assert r.status_code == 403

    def test_default_scene_custom_agent_403(self):
        cid = _seed_issue(issue_number=66, scene_id="default")
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(), json={"review_points": "x"})
        assert r.status_code == 403

    def test_scene_resolve_failure_falls_back(self):
        scene_id = _seed_scene()
        cid = _seed_issue(issue_number=67, scene_id=scene_id)
        with patch("src.api.pipeline_config.get_pipeline_config", side_effect=RuntimeError("db down")):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(), json={"review_points": "x"})
        assert r.status_code == 403

    def test_unbound_scene_original_roles_unchanged(self):
        cid = _seed_issue(issue_number=68, scene_id="")
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/awaiting-input",
                            headers=_session_headers(agent_type="issue"), json={"questions": "q"})
        assert r.status_code == 200 and r.json()["changed"] is True
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(agent_type="qa"), json={"review_points": "x"})
        assert r.status_code == 200
