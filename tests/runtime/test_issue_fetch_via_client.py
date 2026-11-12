"""S28/D18 用例：管道内 Issue 读取走统一命令层（VcsApiClient 卡驱动）。"""
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


class _FakeResp:
    def __init__(self, data):
        self._data = json.dumps(data).encode()
        self.headers = {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_issue_api(monkeypatch, resp_data):
    seen = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.method, self.headers = url, method, headers or {}

    import urllib.request
    import urllib.error

    def fake_urlopen(req, timeout=30):
        seen.append(req)
        if isinstance(resp_data, urllib.error.HTTPError):
            raise resp_data
        return _FakeResp(resp_data)

    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_s28_gitee_issue_fetch_via_client(gitee_card, monkeypatch):
    """gitee：_vcs_get_issue 命中卡 api_base + query_token（统一命令层）。"""
    from src.domains.runtime.pipeline.event_pipeline import _vcs_get_issue
    seen = _mock_issue_api(monkeypatch, {"number": 5, "title": "T5", "state": "open"})
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="TK"):
        state, d = _vcs_get_issue("gitee:o/r", "5")
    assert state == "exists" and d["title"] == "T5"
    assert seen[-1].url == "https://gitee.com/api/v5/repos/o/r/issues/5?access_token=TK"


def test_s28_github_regression(monkeypatch):
    """github 回归：token 走 get_github_token，endpoint 原样。"""
    from src.domains.runtime.pipeline.event_pipeline import _vcs_get_issue
    seen = _mock_issue_api(monkeypatch, {"number": 3, "title": "GH"})
    with patch("src.domains.mgmt.user.db.get_github_token", return_value="GHTK"):
        state, d = _vcs_get_issue("github:o/r", "3")
    assert state == "exists"
    assert seen[-1].url == "https://api.github.com/repos/o/r/issues/3"
    assert seen[-1].headers.get("Authorization") == "token GHTK"


def test_s28_missing_and_unknown(monkeypatch):
    from src.domains.runtime.pipeline.event_pipeline import _vcs_get_issue, _fetch_issue_title_via_api
    import urllib.error
    # 404 → missing（映射保持）
    _mock_issue_api(monkeypatch, urllib.error.HTTPError(
        "u", 404, "Not Found", {}, io_bytes := type("B", (), {"read": lambda s: b"{}"})()))
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="TK"):
        assert _vcs_get_issue("gitcode:o/r", "9")[0] == "missing"
        assert _fetch_issue_title_via_api("gitcode:o/r", "9") == ""
    # 无 token → unknown
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value=""):
        assert _vcs_get_issue("gitcode:o/r", "9")[0] == "unknown"


def test_s28_title_fetch_gitee(gitee_card, monkeypatch):
    from src.domains.runtime.pipeline.event_pipeline import _fetch_issue_title_via_api
    _mock_issue_api(monkeypatch, {"number": 7, "title": "Gitee T7"})
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="TK"):
        assert _fetch_issue_title_via_api("gitee:o/r", "7") == "Gitee T7"
        assert _fetch_issue_title_via_api("gitee:o/r", "") == ""
