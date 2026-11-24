"""Issue #4065：`/internal/issues/card` 对非数字 ident 的可读结果（不再裸 422）。

生产：`GET /internal/issues/card?issue_number=autopilot-as` 恒 422（FastAPI 把
`issue_number: int` 强转失败），244 条/小时、4 台主控全中——卡片刷新与 stage 解析全断。

口径（与 #3999 收敛）：先归一，可归一（数字 / 平台 ident / `autopilot-*` 合成键）→ 正常卡片；
无法归一（`pr_*` 订阅伪 id、空、含分隔文本、超长）→ 400 + 可读原因。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeRuntime:
    """替身 runtime 服务：只回答 get_issue_card。"""

    def __init__(self, cards=None):
        self.cards = cards or {}
        self.calls = []

    def get_issue_card(self, repo_key, issue_number):
        self.calls.append((repo_key, issue_number))
        return self.cards.get(str(issue_number), {})


@pytest.fixture
def client(monkeypatch):
    from src.domains.runtime.routes import internal as internal_route

    fake = _FakeRuntime({"autopilot-as": {"stage": "done"},
                         "IKX1DL": {"stage": "coding"},
                         "4065": {"stage": "coding"}})
    monkeypatch.setattr(internal_route, "_get_local", lambda _name: fake)
    app = FastAPI()
    app.include_router(internal_route.router)
    return TestClient(app), fake


class TestIssueCardIdentHandling:
    def test_autopilot_synthetic_key_is_readable(self, client):
        c, fake = client
        r = c.get("/internal/issues/card", params={"repo_key": "gitcode:grape-dev/grape",
                                                  "issue_number": "autopilot-as"})
        assert r.status_code == 200, r.text
        assert r.json()["stage"] == "done"
        assert ("gitcode:grape-dev/grape", "autopilot-as") in fake.calls

    def test_gitee_native_ident_is_readable(self, client):
        c, fake = client
        r = c.get("/internal/issues/card", params={"repo_key": "gitee:GTDTeam/CodeArts",
                                                   "issue_number": "IKX1DL"})
        assert r.status_code == 200, r.text
        assert r.json()["stage"] == "coding"

    def test_numeric_behaviour_unchanged(self, client):
        c, fake = client
        r = c.get("/internal/issues/card", params={"repo_key": "gitcode:grape-dev/grape",
                                                   "issue_number": "4065"})
        assert r.status_code == 200, r.text
        assert r.json()["stage"] == "coding"

    @pytest.mark.parametrize("ident", ["pr_123", "#pr_123_scene_id", "", "   "])
    def test_pr_pseudo_ident_returns_readable_400(self, client, ident):
        c, fake = client
        r = c.get("/internal/issues/card", params={"repo_key": "gitcode:grape-dev/grape",
                                                   "issue_number": ident})
        assert r.status_code == 400, r.text
        body = r.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        assert detail.get("error") == "invalid_issue_ident"
        assert "issue_number" in detail.get("message", "")
        assert fake.calls == [], "不可归一 ident 不得进入查询层"

    def test_garbage_ident_returns_readable_400(self, client):
        c, _fake = client
        r = c.get("/internal/issues/card", params={"repo_key": "gitcode:grape-dev/grape",
                                                   "issue_number": "autopilot as"})
        assert r.status_code == 400, r.text


class TestSessionStoreIdentNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("autopilot-as", "autopilot-as"),
        ("IKX1DL", "IKX1DL"),
        ("#4065", 4065),
        ("pr_123", None),
    ])
    def test_issue_num_or_ident(self, raw, expected):
        from src.api.session_store import _issue_num_or_ident
        assert _issue_num_or_ident(raw) == expected

    def test_pr_pseudo_ident_never_hits_provider(self, monkeypatch):
        from src.api import session_store
        import src.kernel.gsb.provider as provider
        called = []
        monkeypatch.setattr(provider, "get_service",
                            lambda name: called.append(name) or _FakeRuntime())
        assert session_store.get_issue_stage("gitcode:grape-dev/grape", "pr_123") is None
        assert called == [], "pr_* 伪 id 不应触达卡片端点（24/h 422 来源）"