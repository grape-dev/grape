"""Issue #2508 session-token auth failure classes (kernel side).

Split out of tests/runtime because the code under test is
src/kernel/auth_sdk/auth_token.py + src/api/deps.py classification, and
tests/README maps src/kernel/** to tests/kernel/**.
"""
import uuid  # noqa: F401

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.kernel.session_manager import db, db_lock

REPO_KEY = "gitcode:issue2508/r"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM session_api_tokens WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:issue2508/%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i2508rt-%'")
        db.commit()


def _seed_task(task_id, event_seq, status, agent="coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
            "issue_id, repo_key, agent_type, prompt, status) "
            "VALUES (?,?,0,?,?,?,?,'p',?)",
            (task_id, "i2508sess-" + agent, event_seq, "#6401", REPO_KEY, agent, status))
        db.commit()


def _probe_client():
    app = FastAPI()

    @app.get("/probe")
    def probe(user: dict = Depends(get_current_user_or_api_key)):
        return {"ok": True}

    return TestClient(app)


def _token_for_task(task_id, status):
    from src.api.auth_token import issue_session_token, _sha256
    _seed_task(task_id, 0, status)
    token = issue_session_token(session_key=task_id, repo_key=REPO_KEY, agent_type="coding")
    with db_lock:
        db.cursor().execute(
            "UPDATE session_api_tokens SET expires_at='0' WHERE token_sha256=?",
            (_sha256(token),))
        db.commit()
    return token


def test_401_revoked_or_finished_task_code():
    _clean()
    token = _token_for_task("t_i2508finished", "completed")
    r = _probe_client().get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["code"] == 401
    assert detail["auth_code"] == "TOKEN_REVOKED_OR_TASK_FINISHED"
    assert detail["action"] == "stop_task_no_recovery"
    assert detail["error_code"] == "SESSION_TOKEN_INVALID"
    assert "re-read GRAPE_SESSION_TOKEN" not in detail["message"]


def test_401_snapshot_expired_code():
    _clean()
    token = _token_for_task("t_i2508running", "running")
    r = _probe_client().get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["code"] == 401
    assert detail["auth_code"] == "SNAPSHOT_EXPIRED"
    assert detail["action"] == "reload_runtime_env"
    assert "re-read GRAPE_SESSION_TOKEN" in detail["message"]


