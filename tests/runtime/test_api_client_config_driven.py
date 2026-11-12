"""S14/D13：VCS API 命令层配置驱动——gitee 走 VcsApiClient 零代码命令面。"""
import io
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.api_client import VcsApiClient
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


def _calls(monkeypatch):
    """捕获 urllib urlopen 的 (url, method, headers, body)。"""
    seen = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.data, self.method, self.headers = url, data, method, headers or {}

    def fake_urlopen(req, timeout=30):
        seen.append(req)
        return _FakeResp({"id": 77, "number": 5, "title": "t", "state": "open"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request, "Request", _Req)
    return seen


def test_s14_comment_hits_card_endpoint(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    resp = c.post_issue_comment(5, "hello")
    assert "error" not in resp
    req = seen[-1]
    assert req.url == ("https://gitee.com/api/v5/repos/oschina/grape/issues/5/comments"
                       "?access_token=TK")
    assert req.method == "POST"
    assert json.loads(req.data) == {"body": "hello"}


def test_s14_get_issue_and_repo_info(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    c.get_issue(5)
    assert seen[-1].url == "https://gitee.com/api/v5/repos/oschina/grape/issues/5?access_token=TK"
    c.get_repo_info()
    assert seen[-1].url == "https://gitee.com/api/v5/repos/oschina/grape?access_token=TK"


def test_s14_create_pr_card(gitee_card, monkeypatch):
    seen = _calls(monkeypatch)
    c = VcsApiClient("gitee", "TK", "oschina", "grape")
    c.create_pr("T", "feat", "main", "b")
    req = seen[-1]
    assert req.url == "https://gitee.com/api/v5/repos/oschina/grape/pulls?access_token=TK"
    assert json.loads(req.data)["head"] == "feat"


def test_s14_gitlab_dialect_new_platform_paths(monkeypatch, gitee_card):
    """gitlab 方言新平台（如自建 GLS）：project 编码路径 + Bearer 鉴权（卡驱动）。"""
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gls": {"dialect": "gitlab", "host": "gls.internal",
                                 "api_base": "https://gls.internal/api/v4",
                                 "api_auth": "bearer_header"}}),))
        db.commit()
    invalidate_platform_card_cache()
    try:
        seen = _calls(monkeypatch)
        c = VcsApiClient("gls", "TK", "grp", "repo")
        c.post_issue_comment(9, "x")
        req = seen[-1]
        assert req.url == "https://gls.internal/api/v4/projects/grp%2Frepo/issues/9/notes"
        assert req.headers.get("Authorization") == "Bearer TK"
        c.create_pr("T", "h", "b", "")
        assert seen[-1].url == "https://gls.internal/api/v4/projects/grp%2Frepo/merge_requests"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()


def test_s14_existing_providers_request_style_unchanged(monkeypatch):
    """存量三平台请求实现原样（S9 回归：gitcode query_token / github token header / gitlab PRIVATE-TOKEN）。"""
    seen = _calls(monkeypatch)
    VcsApiClient("gitcode", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://gitcode.com/api/v5/repos/o/r/issues/1?access_token=TK"
    VcsApiClient("github", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://api.github.com/repos/o/r/issues/1"
    assert seen[-1].headers.get("Authorization") == "token TK"
    VcsApiClient("gitlab", "TK", "o", "r").get_issue(1)
    assert seen[-1].url == "https://gitlab.com/api/v4/projects/o%2Fr/issues/1"
    assert seen[-1].headers.get("PRIVATE-TOKEN") == "TK"


def test_s14_unsupported_provider_rejected(monkeypatch):
    _calls(monkeypatch)
    c = VcsApiClient("nonexist", "TK", "o", "r")
    resp = c.get_issue(1)
    assert "error" in resp and "unsupported" in resp["error"]
