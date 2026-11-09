"""A2A 重新派发（a2a-redo）端点测试。

见 docs/weekly-06/a2a-redo-button-design.md §7 验收标准。
只测新端点；不跑全量。直接调用端点函数（绕过 HTTP 认证层），
用与 test_a2a_scene_check.py 相同的 sqlite 落数方式。
"""
import os
import sys
import json
import time
import asyncio

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_a2a_redo.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi import HTTPException

from src.api.db_setup import init_database
from src.session_manager import db, db_lock
from src.api.routes.events import a2a_redo_event, A2ARedoBody
from src.api.routes.webhook import _scene_node_check

init_database()

REPO = "gitcode:org/a2aredo"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        db.commit()


def _mk_event(status="completed", issue_id="#7", agent_type="coding"):
    from src.api.event_store import append_event
    with db_lock:
        db.cursor().execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()
    return append_event(repo_key=REPO, event_type="issue_comment", title="t",
                        issue_id=issue_id, source="gitcode", agent_type=agent_type,
                        actor="u1", summary="s", payload={}, status=status)


def _user(auth="jwt"):
    return {"id": 1, "username": "tester", "auth_type": auth}


def _run(seq, agent, user=None):
    return asyncio.get_event_loop().run_until_complete(
        a2a_redo_event(seq, A2ARedoBody(agent=agent), user or _user()))


def test_invalid_agent():
    with pytest.raises(HTTPException) as ei:
        _run(1, "hacker")
    assert ei.value.status_code == 400


def test_not_completed_rejected():
    seq = _mk_event(status="running")
    with pytest.raises(HTTPException) as ei:
        _run(seq, "qa")
    assert ei.value.status_code == 409


def test_pr_pseudo_issue_rejected():
    seq = _mk_event(status="completed", issue_id="#pr_12_scn")
    with pytest.raises(HTTPException) as ei:
        _run(seq, "qa")
    assert ei.value.status_code == 400


def test_repo_access_rejected():
    """非归属人（jwt 用户 get_managed_repos 无该仓库）→ 403。"""
    from unittest.mock import patch
    seq = _mk_event()
    with patch("src.user.db.get_managed_repos", return_value=[]):
        with pytest.raises(HTTPException) as ei:
            _run(seq, "qa")
    assert ei.value.status_code == 403
