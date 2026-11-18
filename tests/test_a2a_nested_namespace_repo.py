"""A2A 端点支持嵌套 namespace 仓库 + 400 拒绝原因可诊断。

线上形态（2026-11-17 生产日志）：仓库 `biz/jmbos/devops/ops-inspection`（4 段），
Issue 链路正常，但 `POST /api/a2a` 与 `GET /api/a2a/status` 恒 400：
两个端点都用 `repository.count("/") != 1` 校验，把嵌套 namespace 判成非法格式，
任务链断在 A2A 交棒那一步。平台其他入口（`POST /api/issues/{full_name}`、
workspace_repo_dir、managed_repos.repo_key）本来就支持嵌套，因此这里对齐放宽为
"段数 >= 2 且无空段"，仍拒绝裸名与 `a//b` 这类畸形输入。

同时覆盖可诊断性：各 400 分支统一走 `_a2a_reject`，把 reason 打进日志，
避免运维只看到 `status=400` 无从判断是缺字段还是格式错。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402

NESTED = "biz/jmbos/devops/ops-inspection"
NESTED_KEY = f"gitcode:{NESTED}"
FLAT = "owner/repo"
FLAT_KEY = f"gitcode:{FLAT}"


def _clean():
    with db_lock:
        c = db.cursor()
        for key in (NESTED_KEY, FLAT_KEY):
            c.execute("DELETE FROM events WHERE repo_key=?", (key,))
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (key,))
            c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (key,))
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key=?", (key,))
            except Exception:
                pass
        db.commit()


def _seed(repo_key: str, full_name: str):
    with db_lock:
        c = db.cursor()
        parts = full_name.split("/")
        try:
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES (?,?,?,?)", (repo_key, parts[0], "/".join(parts[1:]), full_name))
            db.commit()
        except Exception:
            pass


def _body(repo: str, agent: str = "coding", issue_number: str = "12"):
    return {"agent": agent,
            "payload": {"repository": repo, "account": repo,
                        "issue_number": issue_number, "pr_number": "",
                        "context": "嵌套 namespace 回归"}}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.routes import webhook as wh
    wh._A2A_REJECT_HITS.clear()
    c = TestClient(app)
    _clean()
    yield c
    _clean()
    wh._A2A_REJECT_HITS.clear()


class TestNestedNamespaceAccepted:
    def test_post_a2a_nested_namespace_creates_event(self, client):
        """嵌套 namespace 仓库必须能交棒（不能因 4 段就 400）。"""
        _seed(NESTED_KEY, NESTED)
        r = client.post("/api/a2a", json=_body(NESTED))
        assert r.status_code == 200, r.text
        assert r.json().get("seq", 0) > 0, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_key FROM events WHERE repo_key=? AND event_type='a2a_event'"
                " ORDER BY seq DESC LIMIT 1", (NESTED_KEY,)).fetchone()
        assert row and row[0] == NESTED_KEY, "事件应落在嵌套仓库的 repo_key 上"

    def test_status_endpoint_accepts_nested_namespace(self, client):
        """核对接口同口径：嵌套 namespace 不得被当成非法格式 400。"""
        _seed(NESTED_KEY, NESTED)
        r = client.get("/api/a2a/status",
                       params={"repository": NESTED, "issue_number": "12"})
        assert r.status_code == 200, r.text
        assert "events" in r.json()

    def test_flat_repo_still_accepted(self, client):
        """两段 owner/repo 行为不变（放宽不得破坏既有形态）。"""
        _seed(FLAT_KEY, FLAT)
        r = client.post("/api/a2a", json=_body(FLAT))
        assert r.status_code == 200, r.text


class TestMalformedRepositoryRejected:
    @pytest.mark.parametrize("bad", ["bare-name", "a//b", "/leading", "trailing/", ""])
    def test_invalid_shapes_still_rejected(self, client, bad):
        """裸名/空段仍必须 400（放宽不等于不校验）。"""
        r = client.post("/api/a2a", json=_body(bad))
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "repository" in str(r.json().get("detail", {})).lower()


class TestRepoKeyProviderProbe:
    """repo_key 反查必须三平台全探（gitlab 漏探会让 GitLab 仓恒解成 gitcode:）。"""

    GITLAB_KEY = f"gitlab:{NESTED}"

    def _clean_gitlab(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (self.GITLAB_KEY,))
            db.commit()

    def test_resolve_a2a_repo_key_probes_gitlab(self):
        """GitLab 托管的嵌套仓库：无前缀传入必须解成 gitlab:，不能回落 gitcode:。"""
        from src.api.routes.webhook import _resolve_a2a_repo_key
        self._clean_gitlab()
        try:
            _seed(self.GITLAB_KEY, NESTED)
            assert _resolve_a2a_repo_key(NESTED) == self.GITLAB_KEY, (
                "只探 github:/gitcode: 时这里会回落 gitcode: → 可达性回退拿 GitCode "
                "去探 GitLab 仓 → 404 → 400 repo_not_found")
        finally:
            self._clean_gitlab()

    def test_resolve_a2a_repo_key_prefixed_passthrough(self):
        """已带前缀的输入原样归一，不因多探一个前缀而改变。"""
        from src.api.routes.webhook import _resolve_a2a_repo_key
        assert _resolve_a2a_repo_key(f"gitlab:{NESTED}") == self.GITLAB_KEY
        assert _resolve_a2a_repo_key(NESTED_KEY) == NESTED_KEY


class TestRejectReasonIsDiagnosable:
    def test_empty_body_returns_actionable_400(self, client):
        """空 body：原来是框架 422（"Field required"），改为可操作的 400。"""
        r = client.post("/api/a2a")
        assert r.status_code == 400, (r.status_code, r.text)
        msg = str(r.json().get("detail", {}).get("message", ""))
        assert "empty body" in msg and "payload" in msg, msg

    def test_reject_reason_logged(self, client, caplog):
        """400 必须在日志里带 reason：否则运维只看到 status=400，只能靠猜。"""
        import logging
        caplog.set_level(logging.WARNING, logger="devops-bot")
        client.post("/api/a2a", json=_body("bare-name"))
        msgs = [r.getMessage() for r in caplog.records]
        assert any("A2A rejected (400)" in m and "reason=repo_format" in m for m in msgs), msgs

    def test_reject_hits_deduped(self, client):
        """同一拒绝原因累计计数，而不是每条都刷日志（首现 + 每 50 次）。"""
        from src.api.routes import webhook as wh
        for _ in range(3):
            client.post("/api/a2a", json=_body("bare-name"))
        assert sum(wh._A2A_REJECT_HITS.values()) >= 3, wh._A2A_REJECT_HITS
