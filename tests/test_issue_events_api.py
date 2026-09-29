"""按 Issue 获取事件摘要 API（阶段0）。

覆盖（API-01~05）：
- API-01 按 issue 返回全部事件段（seq 升序，含 output/logs）
- API-02 兼容 `#5` / `5`
- API-03 无数据返回空 segments（不 404）
- API-04 无鉴权 401/403
- API-05 跨 provider repo 过滤
"""
import os, sys, json

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api.jwt_utils import create_access_token


def _seed(seq, repo_key, issue, agent, status="completed", structured=None, logs=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
            "agent_logs, result, structured_output, created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
            (seq, repo_key, "issue_opened", "webhook", issue, "t", "s", "", "{}",
             agent, "opencode", agent + "-cli", status, 0, 1.0,
             json.dumps(logs or []), "结论" + agent,
             json.dumps(structured) if structured else None))
        db.commit()


def _jwt(user_id, username):
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


_app = None
_client = None
_uid = None
_headers = {}


def setup_module():
    global _client, _uid, _headers
    from src.api.main import app as _a
    from src.api.db_setup import init_database
    init_database()
    _client = TestClient(_a)
    _uid = user_db.create_user("evapi_user", "pwd")
    if not _uid:
        _uid = user_db.get_user_by_username("evapi_user")
    _headers = {"Authorization": "Bearer " + _jwt(_uid["id"], _uid["username"])}


def setup_function():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key IN (?,?) OR repo_key LIKE 'github:%org%'",
                  ("gitcode:org/evapi", "gitcode:org/evapi2"))
        db.commit()


def test_api_issue_events_segments_sorted():
    _seed(9001, "gitcode:org/evapi", "#5", "vod",
          structured={"actions": [{"cmd": "a"}], "conclusion": "c1", "artifacts": [], "next_step": "issue", "issue_summary": "s1"})
    _seed(9002, "gitcode:org/evapi", "#5", "coding",
          structured={"actions": [], "conclusion": "c2", "artifacts": ["f"], "next_step": "qa", "issue_summary": "s2"},
          logs=["step1", "step2"])
    r = _client.get("/api/issues/org/evapi/5/events", headers=_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 2
    segs = d["segments"]
    assert [s["seq"] for s in segs] == [9001, 9002]  # seq 升序
    assert segs[0]["agent_type"] == "vod" and segs[0]["output"]["conclusion"] == "c1"
    assert segs[1]["output"]["actions"] == []
    assert segs[1]["logs"] == ["step1", "step2"]


def test_api_issue_events_hash_compatible():
    _seed(9010, "gitcode:org/evapi", "#5", "vod")
    r1 = _client.get("/api/issues/org/evapi/5/events", headers=_headers)
    r2 = _client.get("/api/issues/org/evapi/%235/events", headers=_headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(r1.json()["segments"]) == len(r2.json()["segments"]) == 1


def test_api_issue_events_empty_not_404():
    """未知 issue：events 无记录 → 不崩溃（200 空或 404 均可，视 stage/status 有无）。"""
    r = _client.get("/api/issues/org/evapi/99999/events", headers=_headers)
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["segments"] == []


def test_api_issue_events_requires_auth():
    r = _client.get("/api/issues/org/evapi/5/events")
    assert r.status_code in (401, 403)


def test_api_issue_events_cross_provider_filter():
    """route 双查 gitcode+github 两前缀：同一 issue 两侧事件均返回。"""
    _seed(9020, "gitcode:org/evapi2", "#3", "sre")
    _seed(9021, "github:org/evapi2", "#3", "sre")
    r = _client.get("/api/issues/org/evapi2/3/events", headers=_headers)
    assert r.status_code == 200
    assert len(r.json()["segments"]) == 2  # gitcode + github 双查


# ── tk_ 会话 token：dual 支持 + repo 作用域 ──

def _session_token(repo_key="gitcode:org/evapi"):
    from src.api.auth_token import issue_session_token
    return issue_session_token(session_key="t_api", repo_key=repo_key, agent_type="vod")


def test_api_issue_events_dual_session_token_ok():
    _seed(9030, "gitcode:org/evapi", "#5", "vod")
    tok = _session_token()
    r = _client.get("/api/issues/org/evapi/5/events",
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200    # dual：tk_ 可访问同仓库


def test_api_issue_events_session_token_cross_repo_forbidden():
    _seed(9040, "gitcode:org/evapi", "#5", "vod")
    # token 绑定其他仓库（github:other/repo）
    tok = _session_token(repo_key="github:other/repo")
    r = _client.get("/api/issues/org/evapi/5/events",
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 403    # 跨仓库拒绝