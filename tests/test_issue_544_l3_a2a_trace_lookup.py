"""Issue #544 L3：A2A 失败留痕 + 交接核对接口。

覆盖（见 docs/issue-544/plan.md L3）：
- 请求处理异常（建事件之前）→ events 表写入失败留痕记录（含请求指纹，status=failed），
  响应 500 带指纹；
- 正常 A2A 事件 payload 带指纹；
- `GET /api/a2a/status` 核对接口：按幂等键 / 按指纹（仓库+issue+agent）查询交接是否已建事件。

测试 seam：events 行 + `/api/a2a/status` HTTP 响应。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


class TestA2ATraceAndLookup:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-a2a-%'")
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            try:
                c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            except Exception:
                pass
            db.commit()

    def _seed(self):
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
                db.commit()
            except Exception:
                pass

    def _body(self, agent="coding", issue_number="5", key=None):
        body = {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": issue_number,
                "pr_number": "",
                "context": "测试 A2A 留痕",
            },
        }
        if key:
            body["idempotency_key"] = key
        return body

    def test_exception_before_event_writes_trace(self, monkeypatch):
        """建事件前异常 → 失败留痕事件（含指纹）+ 500 响应带指纹。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        import src.api.routes.webhook as wh
        self._clean()
        self._seed()
        # 在 append_event 之前制造异常：锁 _authorize_a2a 之后必然经过的内部函数
        monkeypatch.setattr(wh, "_scene_node_check",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom-544")))
        client = TestClient(app)
        r = client.post("/api/a2a", json=self._body())
        assert r.status_code == 500, (r.status_code, r.text)
        fingerprint = r.json().get("fingerprint", "")
        assert fingerprint, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT status, payload, event_type, agent_type FROM events"
                " WHERE repo_key='gitcode:owner/repo' AND event_type='a2a_event'"
                " ORDER BY seq DESC LIMIT 1").fetchone()
        assert row, "异常路径必须留痕（Issue #544 L3）"
        assert row[0] == "failed"
        import json as _json
        payload = _json.loads(row[1] or "{}")
        assert payload.get("a2a_trace") is True
        assert payload.get("fingerprint") == fingerprint
        assert row[3] == "coding"

    def test_normal_event_carries_fingerprint(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r = client.post("/api/a2a", json=self._body())
        assert r.status_code == 200 and r.json().get("seq", 0) > 0, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT payload FROM events WHERE repo_key='gitcode:owner/repo'"
                " AND event_type='a2a_event' ORDER BY seq DESC LIMIT 1").fetchone()
        import json as _json
        payload = _json.loads(row[0] or "{}")
        assert payload.get("fingerprint"), "正式 A2A 事件应带请求指纹"

    def test_status_lookup_by_fingerprint(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r = client.post("/api/a2a", json=self._body())
        seq = r.json()["seq"]
        s = client.get("/api/a2a/status", params={
            "repository": "owner/repo", "issue_number": "5", "agent": "coding"})
        assert s.status_code == 200, s.text
        j = s.json()
        assert j.get("found") is True, j
        seqs = [e["seq"] for e in j.get("events", [])]
        assert seq in seqs, j

    def test_status_lookup_by_idempotency_key(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r = client.post("/api/a2a", json=self._body(key="task-77:qa"))
        seq = r.json()["seq"]
        s = client.get("/api/a2a/status", params={
            "repository": "owner/repo", "issue_number": "5",
            "idempotency_key": "task-77:qa"})
        assert s.status_code == 200, s.text
        j = s.json()
        assert j.get("found") is True and seq in [e["seq"] for e in j.get("events", [])], j

    def test_status_lookup_miss(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        s = client.get("/api/a2a/status", params={
            "repository": "owner/repo", "issue_number": "5", "agent": "sre"})
        assert s.status_code == 200, s.text
        assert s.json().get("found") is False, s.text

    def test_status_resolves_github_repo_key(self):
        """审查修正②：GET status 的 repo_key 解析须与 POST /api/a2a 一致。

        github 托管仓以无前缀 owner/repo 传入时，应经 managed_repos 反查得到
        github: 前缀，指纹按 github: repo_key 计算，核对不得假阴性。
        """
        import hashlib
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.event_store import append_event
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'github:owner/ghrepo%'")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'github:owner/ghrepo%'")
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES ('github:owner/ghrepo', 'owner', 'ghrepo', 'owner/ghrepo')")
            db.commit()
        _fp = hashlib.sha256(
            "github:owner/ghrepo|5|coding".encode()).hexdigest()[:16]
        seq = append_event(
            repo_key="github:owner/ghrepo", event_type="a2a_event", title="t",
            issue_id="#5", agent_type="coding", source="a2a",
            payload={"fingerprint": _fp})
        assert seq > 0
        client = TestClient(app)
        s = client.get("/api/a2a/status", params={
            "repository": "owner/ghrepo", "issue_number": "5", "agent": "coding"})
        assert s.status_code == 200, s.text
        j = s.json()
        assert j.get("found") is True, j
        assert seq in [e["seq"] for e in j.get("events", [])], j
