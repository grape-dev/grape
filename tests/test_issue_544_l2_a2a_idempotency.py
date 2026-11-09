"""Issue #544 L2：/api/a2a 幂等键——重复请求返回已有事件，不创建新事件。

覆盖（见 docs/issue-544/plan.md L2）：
- 同键两次 POST → 第二次返回 `idempotent: true` + 原 seq，events 只多一条；
- 键放 payload.idempotency_key 同样生效（body 顶层优先）；
- 不同键 → 各自建事件；
- 非法键（ charset/长度）→ 400；
- 无键请求不受影响（现状回归）。

测试 seam：`/api/a2a` HTTP 响应 + events 表行数（公开行为）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


class TestA2AIdempotency:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            try:
                c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            except Exception:
                pass
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

    def _body(self, agent="coding", issue_number="5", key=None, key_in_payload=False):
        body = {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": issue_number,
                "pr_number": "",
                "context": "测试 A2A 幂等键",
            },
        }
        if key is not None:
            if key_in_payload:
                body["payload"]["idempotency_key"] = key
            else:
                body["idempotency_key"] = key
        return body

    def _post(self, client, body):
        return client.post("/api/a2a", json=body)

    def _a2a_event_count(self):
        with db_lock:
            return db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE event_type='a2a_event'"
                " AND repo_key LIKE 'gitcode:owner/repo%'").fetchone()[0]

    def test_same_key_returns_existing_event(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r1 = self._post(client, self._body(key="task-42:qa"))
        assert r1.status_code == 200, r1.text
        assert r1.json().get("seq", 0) > 0, r1.text
        seq1 = r1.json()["seq"]
        r2 = self._post(client, self._body(key="task-42:qa"))
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2.get("idempotent") is True, j2
        assert j2.get("seq") == seq1, j2
        assert self._a2a_event_count() == 1, "同键重复请求不得创建新事件"

    def test_key_in_payload_also_works(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r1 = self._post(client, self._body(key="t1:sre", key_in_payload=True))
        assert r1.json().get("seq", 0) > 0
        r2 = self._post(client, self._body(key="t1:sre", key_in_payload=True))
        assert r2.json().get("idempotent") is True
        assert r2.json()["seq"] == r1.json()["seq"]
        assert self._a2a_event_count() == 1

    def test_different_keys_create_separate_events(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        # 不同键（配合不同 issue，避开既有的同 issue+agent 运行中去重）
        r1 = self._post(client, self._body(key="k1", issue_number="5"))
        r2 = self._post(client, self._body(key="k2", issue_number="6"))
        assert r1.json()["seq"] > 0 and r2.json()["seq"] > 0
        assert r1.json()["seq"] != r2.json()["seq"]
        assert self._a2a_event_count() == 2

    def test_invalid_key_rejected_400(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        # 空串/缺省视为"未携带键"（放行），仅非法非空键 400
        for bad in ("x" * 129, "bad key!", "键值"):
            r = self._post(client, self._body(key=bad))
            assert r.status_code == 400, (bad, r.status_code, r.text)

    def test_no_key_unchanged_behavior(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        self._seed()
        client = TestClient(app)
        r = self._post(client, self._body())
        assert r.status_code == 200 and r.json().get("seq", 0) > 0, r.text
        assert "idempotent" not in r.json()

    def test_idempotent_lookup_requires_auth(self):
        """审查修正①：幂等早退必须在鉴权之后。

        仓库带 bot token（_authorize_a2a 严格校验）时，未认证调用方即使携带
        已存在的幂等键也不得换取已有事件的 seq/status/issue_id（防探测泄露）。
        """
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.event_store import append_event
        self._clean()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_gitcode_token) "
                "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo', 'tok-544')")
            db.commit()
        seq = append_event(
            repo_key="gitcode:owner/repo", event_type="a2a_event", title="t",
            issue_id="#5", agent_type="coding", source="a2a",
            payload={}, idempotency_key="task-auth:qa")
        assert seq > 0
        client = TestClient(app)
        body = self._body(key="task-auth:qa")
        # 无凭据（无 Authorization/X-Bot-Token 头）→ 必须 403，不得 200 幂等命中
        r = client.post("/api/a2a", json=body)
        assert r.status_code == 403, (r.status_code, r.text)
        # 错误 token 同样 403
        r_bad = client.post("/api/a2a", json=body, headers={"X-Bot-Token": "wrong-tok"})
        assert r_bad.status_code == 403, (r_bad.status_code, r_bad.text)
        # 正确 bot token → 200 幂等命中返回已有事件
        r_ok = client.post("/api/a2a", json=body, headers={"X-Bot-Token": "tok-544"})
        assert r_ok.status_code == 200, (r_ok.status_code, r_ok.text)
        j = r_ok.json()
        assert j.get("idempotent") is True and j.get("seq") == seq, j
