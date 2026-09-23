"""GRAPE_SCENE_ID 环境变量注入测试（issue-219：场景标识统一 env 注入）。

覆盖：issue 场景来源 / 仓库级 dev_scene 回退 / 空场景注入空串 /
custom_fields（GRAPE_CF_* / GRAPE_CF_MAP）沿用。"""
import json
import pytest

from src.session_manager import db, db_lock

RK = "gitcode:org/sceneenv"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()


def _seed_repo(dev_scene=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, dev_scene) "
            "VALUES (?,?,?,?,?)",
            (RK, "org", "sceneenv", "sceneenv", dev_scene))
        db.commit()


def _seed_issue(issue_number, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) VALUES (?,?,?,?,?)",
            (0, "", RK, issue_number, scene_id))
        db.commit()


def _seed_scene(scene_id, advanced_options=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,?,?)",
            (scene_id, "DevOps Scene", RK, "[]", 1, advanced_options or "{}"))
        db.commit()


def _inject(issue_id="5"):
    from src.api.event_pipeline import _inject_scene_env
    env_extra = {}
    _inject_scene_env(env_extra, RK, issue_id)
    return env_extra


def test_injects_scene_id_and_custom_fields_from_issue_scene():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"work_branch": "main", "custom_fields": [{"key": "env", "value": "prod"}]}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "devops-scene"
    assert env["GRAPE_CF_1"] == "prod"
    assert json.loads(env["GRAPE_CF_MAP"]) == {"GRAPE_CF_1": "env"}


def test_fallback_to_repo_dev_scene():
    _seed_repo("repo-scene")
    _seed_scene("repo-scene", '{"custom_fields": []}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "repo-scene"
    assert "GRAPE_CF_MAP" not in env


def test_empty_when_no_scene():
    env = _inject()
    assert "GRAPE_SCENE_ID" in env
    assert env["GRAPE_SCENE_ID"] == ""


def test_scene_with_work_branch_only_sets_scene_id():
    _seed_issue(5, "devops-scene")
    _seed_scene("devops-scene", '{"work_branch": "main"}')
    env = _inject()
    assert env["GRAPE_SCENE_ID"] == "devops-scene"
    assert "GRAPE_CF_1" not in env
    assert "GRAPE_CF_MAP" not in env


def test_resolve_scene_id_helper():
    from src.api.event_pipeline import _resolve_scene_id
    assert _resolve_scene_id(RK, "5") == ""          # 无场景
    _seed_issue(5, "devops-scene")
    assert _resolve_scene_id(RK, "#5") == "devops-scene"   # issue 场景（# 前缀兼容）
    assert _resolve_scene_id(RK, "") == ""           # 无 issue → 空