"""A2A 场景节点校验测试（a2a-scene-node-check-fix-design.md）。

覆盖：项目场景空 nodes → 拦截；含 coding → 放行；无项目场景行 → 官方判（cloud-native-cli/含 coding）；
跨仓库同 scene_id 隔离（按 repo_key）。直接测 `_scene_node_check`（DB 落库驱动）。
"""
import os
import sys
import json

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_a2a_scene_check.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock
from src.api.routes.webhook import _scene_node_check

init_database()

REPO = "gitcode:org/a"
REPO2 = "gitcode:org/b"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO2))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key IN (?,?)", (REPO, REPO2))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key IN (?,?)", (REPO, REPO2))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key IN (?,?)", (REPO, REPO2))
        db.commit()


def _set_issue(repo_key, number, scene_id):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) VALUES (0,'',?,?,?)",
            (repo_key, number, scene_id))
        db.commit()


def _set_scene(repo_key, scene_id, nodes):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) VALUES (?,?,?,?,1)",
            (scene_id, scene_id, repo_key, json.dumps(nodes)))
        db.commit()


def test_t1_empty_project_scene_blocks():
    _set_issue(REPO, 1, "scene-a")
    _set_scene(REPO, "scene-a", [])
    ck = _scene_node_check(REPO, 1, "coding")
    assert ck["scene_id"] == "scene-a"
    assert ck["in_scene"] is False
    assert ck["nodes"] == []


def test_t2_project_scene_with_coding_allows():
    _set_issue(REPO, 2, "scene-a")
    _set_scene(REPO, "scene-a", [{"id": "coding", "skill": "coding-cli"}])
    ck = _scene_node_check(REPO, 2, "coding")
    assert ck["in_scene"] is True


def test_t2b_project_scene_without_coding_blocks_even_with_other_nodes():
    _set_issue(REPO, 3, "scene-a")
    _set_scene(REPO, "scene-a", [{"id": "qa"}, {"id": "sre"}])
    ck = _scene_node_check(REPO, 3, "coding")
    assert ck["in_scene"] is False


def test_t3_no_project_scene_falls_back_to_official():
    # scene_id=cloud-native-cli 官方含 coding 且无该项目场景行 → 放行（回退语义）
    _set_issue(REPO, 4, "cloud-native-cli")
    ck = _scene_node_check(REPO, 4, "coding")
    assert ck["scene_id"] == "cloud-native-cli"
    assert any(n.get("id") == "coding" for n in ck["nodes"])


def test_t4_cross_repo_same_scene_id_isolated():
    _set_issue(REPO, 5, "scene-x")
    _set_scene(REPO, "scene-x", [{"id": "coding"}])          # repo A 含 coding
    _set_issue(REPO2, 5, "scene-x")
    _set_scene(REPO2, "scene-x", [])                          # repo B 空
    assert _scene_node_check(REPO, 5, "coding")["in_scene"] is True
    assert _scene_node_check(REPO2, 5, "coding")["in_scene"] is False