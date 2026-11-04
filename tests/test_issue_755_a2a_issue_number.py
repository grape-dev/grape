"""Issue #755：跨仓 A2A 转交后 issue_number 在目标仓 VCS 404 —— 单号回填 + 入口校验。

覆盖（见 docs/issue-755/plan.md）：
- _resolve_cross_repo_issue_number：kept / target_field / db_mapping / unresolved 四路
- _issue_exists_in_db：命中与未命中
- _a2a_issue_vcs_check：exists / not_found / unknown（无 token / 网络错）verdict
- _cross_pipeline_handoff：源仓单号回填为真实落地单号（事件 issue_id / carried_context.target_issue_number）
- a2a_endpoint：DB 映射回填（事件落在真实单号下）；VCS 明确 404 → 400 拒绝
- resolve_issue_id：payload 单号 == source_issue_number 且带 target_issue_number → 真实单号
"""
import os
import sys
import json

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_755_a2a_issue_number.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME", "DEVOPS_DB_PORT"):
    os.environ.pop(_k, None)

sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from src.api.db_setup import init_database  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

init_database()

REPO_KEY = "gitcode:org/test-755"
REPO = "org/test-755"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM event_active_guard WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO_KEY,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM event_active_guard WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO_KEY,))
        db.commit()


def _set_issue(number, scene_id="", carried=None, assignee=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, scene_id, carried_context) "
            "VALUES (0, ?, ?, ?, 'processing', ?, ?)",
            (assignee, REPO_KEY, number, scene_id, json.dumps(carried) if carried else ""))
        db.commit()


def _set_scene(scene_id, nodes):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
            "VALUES (?,?,?,?,1)",
            (scene_id, scene_id, REPO_KEY, json.dumps(nodes)))
        db.commit()


# ══════════════════ helper 单测 ══════════════════

def test_issue_exists_in_db():
    from src.api.routes.webhook import _issue_exists_in_db
    _set_issue(755)
    assert _issue_exists_in_db(REPO_KEY, 755) is True
    assert _issue_exists_in_db(REPO_KEY, "755") is True
    assert _issue_exists_in_db(REPO_KEY, 1099) is False
    assert _issue_exists_in_db(REPO_KEY, "abc") is False


def test_resolve_kept_when_issue_in_db():
    """目标仓已有该编号行 → 原样返回（正常同仓 A2A 零行为变化）。"""
    from src.api.routes.webhook import _resolve_cross_repo_issue_number
    _set_issue(755)
    assert _resolve_cross_repo_issue_number(REPO_KEY, "755", {}) == ("755", "kept")


def test_resolve_target_field():
    """carried_context.target_issue_number 显式回填 → 采用。"""
    from src.api.routes.webhook import _resolve_cross_repo_issue_number
    _set_issue(755, carried={"source_issue_number": 1099})
    num, via = _resolve_cross_repo_issue_number(
        REPO_KEY, "1099", {"source_issue_number": 1099, "target_issue_number": 755})
    assert (num, via) == ("755", "target_field")


def test_resolve_db_mapping():
    """DB 反查 carried_context.source_issue_number → 真实落地单号（多命中取最大）。"""
    from src.api.routes.webhook import _resolve_cross_repo_issue_number
    _set_issue(753, carried={"source_issue_number": 1099})
    _set_issue(754, carried={"source_issue_number": 1099})
    num, via = _resolve_cross_repo_issue_number(REPO_KEY, "1099", {})
    assert (num, via) == ("754", "db_mapping")


def test_resolve_unresolved():
    """均未命中 → 原样返回（行为不变，交由方案 B 入口校验）。"""
    from src.api.routes.webhook import _resolve_cross_repo_issue_number
    assert _resolve_cross_repo_issue_number(REPO_KEY, "1099", {}) == ("1099", "unresolved")
    assert _resolve_cross_repo_issue_number(REPO_KEY, "", {}) == ("", "unresolved")
    assert _resolve_cross_repo_issue_number(REPO_KEY, "#1099", {}) == ("1099", "unresolved")


def test_vcs_check_verdicts(monkeypatch):
    from src.api.routes.webhook import _a2a_issue_vcs_check
    monkeypatch.setattr("src.token_resolver.TokenResolver.get_token", lambda self, rk: "tok-755")
    monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_issue", lambda self, n: {"number": n})
    assert _a2a_issue_vcs_check(REPO_KEY, "755") == "exists"
    monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_issue",
                        lambda self, n: {"error": "HTTP 404", "status_code": 404})
    assert _a2a_issue_vcs_check(REPO_KEY, "755") == "not_found"
    monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_issue",
                        lambda self, n: {"error": "conn timeout"})
    assert _a2a_issue_vcs_check(REPO_KEY, "755") == "unknown"
    monkeypatch.setattr("src.token_resolver.TokenResolver.get_token", lambda self, rk: "")
    assert _a2a_issue_vcs_check(REPO_KEY, "755") == "unknown"
    # 无 token 时不外呼
    def _no_call(*a, **kw):
        raise AssertionError("no-token path must not call VCS")
    monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_issue", _no_call)
    assert _a2a_issue_vcs_check(REPO_KEY, "755") == "unknown"


# ══════════════════ _cross_pipeline_handoff 回填 ══════════════════

def test_cross_pipeline_handoff_backfills_source_number():
    """跨仓转交沿用源仓单号（1099）→ 回填为真实落地单号（755），
    事件 issue_id / 派发 payload / carried_context.target_issue_number 全部使用真实单号。"""
    from src.api.routes.webhook import _cross_pipeline_handoff
    _set_issue(755, scene_id="source-scene", carried={"source_issue_number": 1099})
    _set_scene("target-scene", [{"id": "vod"}])
    carried = {"source_issue_number": 1099, "source_scene_id": "source-scene", "rating": "ready"}
    result = _cross_pipeline_handoff(
        REPO_KEY, "1099", "target-scene", "vod",
        carried, "", None, "跨流水线接续测试",
        REPO, REPO,
    )
    assert result["ok"] is True

    # 事件挂在真实单号下（issue_id=#755，非 #1099）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT event_type, payload FROM events WHERE repo_key=? ORDER BY seq DESC",
                  (REPO_KEY,))
        rows = c.fetchall()
    types = [r[0] for r in rows]
    assert "cross_scene_handoff" in types and "a2a_event" in types
    for _t, _p in rows:
        if _t == "a2a_event":
            _payload = json.loads(_p)
            assert _payload.get("issue_number") == "755", _payload
    # carried_context 持久化 target_issue_number
    with db_lock:
        c = db.cursor()
        c.execute("SELECT carried_context FROM issue WHERE repo_key=? AND issue_number=755", (REPO_KEY,))
        _raw = c.fetchone()[0]
    _ctx = json.loads(_raw)
    assert _ctx.get("source_issue_number") == 1099
    assert _ctx.get("target_issue_number") == 755


def test_cross_pipeline_handoff_keeps_existing_number():
    """同仓已有编号（kept）→ 不改写（回归，行为不变）。"""
    from src.api.routes.webhook import _cross_pipeline_handoff
    _set_issue(100, scene_id="source-scene")
    _set_scene("target-scene", [{"id": "vod"}])
    result = _cross_pipeline_handoff(
        REPO_KEY, "100", "target-scene", "vod",
        {"source_issue_number": 100}, "", None, "ctx", REPO, REPO)
    assert result["ok"] is True
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id='#100'", (REPO_KEY,))
        assert c.fetchone()[0] > 0
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=? AND issue_id='#1099'", (REPO_KEY,))
        assert c.fetchone()[0] == 0


# ══════════════════ a2a_endpoint 端点行为 ══════════════════

def _client():
    from fastapi.testclient import TestClient
    from src.api.main import app
    return TestClient(app)


def _session_token():
    from src.api.auth_token import issue_session_token
    return issue_session_token(session_key="sess-755", repo_key=REPO_KEY, agent_type="coding")


def _seed_registered():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES (?,?,?,?)", (REPO_KEY, "org", "test-755", REPO))
        db.commit()


def test_a2a_endpoint_backfills_number(monkeypatch):
    """入口映射回填：A2A 携带源仓单号 1099，目标仓已有 source 映射 → 事件落在 #755。"""
    _seed_registered()
    _set_issue(755, scene_id="", carried={"source_issue_number": 1099})
    # 无 token → VCS unknown → 不阻断；DB 命中后也不外呼
    monkeypatch.setattr("src.token_resolver.TokenResolver.get_token", lambda self, rk: "")
    body = {
        "agent": "coding",
        "payload": {
            "repository": REPO,
            "account": REPO,
            "issue_number": "1099",
            "pr_number": "",
            "context": "跨仓 A2A 转交回填测试",
        },
    }
    r = _client().post("/api/a2a", json=body, headers={"Authorization": f"Bearer {_session_token()}"})
    assert r.status_code == 200, r.text
    with db_lock:
        c = db.cursor()
        c.execute("SELECT issue_id, event_type FROM events WHERE repo_key=? AND event_type='a2a_event'",
                  (REPO_KEY,))
        rows = c.fetchall()
    assert rows and all(rid == "#755" for rid, _ in rows), rows


def test_a2a_endpoint_400_on_vcs_not_found(monkeypatch):
    """方案B：DB 查无 + VCS 明确 404 → 400 拒绝并提示转交建单。"""
    _seed_registered()
    monkeypatch.setattr("src.token_resolver.TokenResolver.get_token", lambda self, rk: "tok-755")
    monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_issue",
                        lambda self, n: {"error": "HTTP 404", "status_code": 404})
    body = {
        "agent": "coding",
        "payload": {
            "repository": REPO,
            "account": REPO,
            "issue_number": "1099",
            "pr_number": "",
            "context": "不存在的源仓单号",
        },
    }
    r = _client().post("/api/a2a", json=body, headers={"Authorization": f"Bearer {_session_token()}"})
    assert r.status_code == 400, r.text
    assert "not found" in r.text and "建单" in r.text, r.text
    # 失败请求不得创建事件
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE repo_key=?", (REPO_KEY,))
        assert c.fetchone()[0] == 0


def test_a2a_endpoint_vcs_unknown_not_blocked(monkeypatch):
    """DB 查无 + VCS unknown（网络/无 token）→ 放行（不误杀正常链路）。"""
    _seed_registered()
    monkeypatch.setattr("src.token_resolver.TokenResolver.get_token", lambda self, rk: "")
    body = {
        "agent": "coding",
        "payload": {
            "repository": REPO,
            "account": REPO,
            "issue_number": "5",
            "pr_number": "",
            "context": "unknown 放行回归",
        },
    }
    r = _client().post("/api/a2a", json=body, headers={"Authorization": f"Bearer {_session_token()}"})
    assert r.status_code == 200, r.text


# ══════════════════ resolve_issue_id 防御性映射 ══════════════════

def test_resolve_issue_id_target_mapping():
    from src.api.event_pipeline import resolve_issue_id
    # payload 单号 == source_issue_number 且带 target_issue_number → 真实单号
    assert resolve_issue_id({
        "issue_number": "1099",
        "carried_context": {"source_issue_number": 1099, "target_issue_number": 755},
    }) == "#755"
    # 正常同仓 A2A（无 carried 标记）→ 原样
    assert resolve_issue_id({"issue_number": "755"}) == "#755"
    # payload 单号与 source 不一致 → 不替换（防污染）
    assert resolve_issue_id({
        "issue_number": "800",
        "carried_context": {"source_issue_number": 1099, "target_issue_number": 755},
    }) == "#800"
    # target 缺失 → 原样
    assert resolve_issue_id({
        "issue_number": "1099",
        "carried_context": {"source_issue_number": 1099},
    }) == "#1099"
