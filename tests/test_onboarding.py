"""Onboarding 首次托管引导测试：场景技能解析、幂等触发、落库、分派、_finalize 补库。"""
import os
import sys
import asyncio
import json
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api import onboarding as ob_mod
from src.api.session_store import get_issue_stage


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos")
        c.execute("DELETE FROM meta WHERE key LIKE 'onboarded:%'")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM issue_state")
        db.commit()


# ===================== dev_scene 读写 =====================

def test_set_get_repo_scene():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "cloud-native"
    assert user_db.get_repo_scene("gitcode:no/such") == "default"
    _clean()


def test_add_managed_repo_dev_scene_default():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    assert user_db.get_repo_scene("gitcode:owner/repo") == "default"
    _clean()


def test_add_managed_repo_dev_scene_explicit():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1, dev_scene="iac")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "iac"
    _clean()


# ===================== 场景 onboarding 技能解析 =====================

def test_scene_onboarding_skill():
    assert ob_mod._scene_onboarding_skill("default") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("cloud-native") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("iac") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("no-such-scene") == ""


# ===================== 幂等标记 =====================

def test_onboarded_mark_idempotent():
    _clean()
    assert ob_mod._is_onboarded("gitcode:owner/repo") is False
    ob_mod._mark_onboarding_started("gitcode:owner/repo")
    assert ob_mod._is_onboarded("gitcode:owner/repo") is True
    _clean()


# ===================== trigger_onboarding =====================

def test_trigger_skips_when_no_skill():
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value=""), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
    _clean()


def test_trigger_skips_when_already_onboarded():
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
    _clean()


def test_trigger_launches_two_guides_direct():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding") as mock_skill, \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "")), \
         patch.object(ob_mod, "_execute_agent_wrapper") as mock_wrap:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_skill.assert_called_with("cloud-native")
        mock_mark.assert_called_once()
        # 两次引导（experience / explore）各触发一次 direct 线程
        assert mock_wrap.call_count == 2
        targets = [c.args[8] for c in mock_wrap.call_args_list]
        assert sorted(targets) == ["experience", "explore"]
    _clean()


def test_trigger_launches_two_guides_queue():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("worker", 7, "")), \
         patch("src.runtime.service.runtime_execution_kind", return_value="queue_local"), \
         patch.object(ob_mod, "_enqueue_onboarding_task") as mock_enq:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        assert mock_enq.call_count == 2
        targets = [c.args[6] for c in mock_enq.call_args_list]
        assert sorted(targets) == ["experience", "explore"]
    _clean()


def test_trigger_creates_two_master_events():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "")), \
         patch.object(ob_mod, "_execute_agent_wrapper"):
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT event_type, issue_id, agent_type, status FROM events WHERE repo_key='gitcode:owner/repo' ORDER BY event_type"
            ).fetchall()
        types = [r[0] for r in rows]
        assert "onboarding_exp" in types and "onboarding_explore" in types
        for r in rows:
            assert r[1] == "#0" and r[2] == "vod" and r[3] == "running"
    _clean()


# ===================== _finalize_onboarding =====================

def test_finalize_creates_stage_and_event(tmp_path):
    _clean()
    rp = os.path.join(str(tmp_path), "onboarding-result-experience.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({"number": 1, "title": "体验", "assignee": "owner", "target": "experience"}, f)
    ob_mod._finalize_onboarding("gitcode:owner/repo", 99, "owner", rp)
    st = get_issue_stage("gitcode:owner/repo", "#1")
    assert st and st["stage"] == "vod" and st["owner_agent"] == "vod" and st["assignee"] == "owner"
    with db_lock:
        row = db.cursor().execute(
            "SELECT event_type, issue_id, agent_type, status FROM events WHERE repo_key='gitcode:owner/repo'"
        ).fetchone()
    assert row and row[0] == "onboarding_issue" and row[1] == "#1" and row[2] == "vod" and row[3] == "completed"
    _clean()


def test_finalize_missing_result_noop():
    _clean()
    ob_mod._finalize_onboarding("gitcode:owner/repo", 99, "owner", "/nonexistent/x.json")
    assert get_issue_stage("gitcode:owner/repo", "#1") is None
    with db_lock:
        cnt = db.cursor().execute("SELECT COUNT(*) FROM events WHERE repo_key='gitcode:owner/repo'").fetchone()[0]
    assert cnt == 0
    _clean()
