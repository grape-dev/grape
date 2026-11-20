"""D35（S55）用例：建单链卡化——查重/建单对 gitee 不再落 GitCode 分支。

先测后改纪律：RED（gitee 查重/建单当前打 GitCode 端点）+ 特征锁
（gitcode/github 存量行为、GitHub Search API、错误语义不变）。
"""
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


def _mock_transport(responses):
    calls = []
    seq = list(responses)

    def _fake(url, method, data=None, headers=None, **kw):
        calls.append({"url": url, "method": method, "data": data, "headers": headers or {}})
        if seq:
            return seq.pop(0)
        return []
    return calls, _fake


# ------------------------------------------------------- S55 查重卡化

def test_s55_find_open_issue_gitee_hits_card_endpoint(gitee_card):
    """gitee 查重：URL 命中卡 api_base（原为 gitcode.com 硬编码 → RED）。"""
    from src.api.issues_center import _find_open_issue_by_title
    calls, fake = _mock_transport([[{"title": "Fix login", "number": 28, "state": "open"}]])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        n = _find_open_issue_by_title("tok-g", "acme", "web", "Fix login", provider="gitee")
    assert n == 28
    assert calls[0]["url"].startswith("https://gitee.com/api/v5/repos/acme/web/issues")
    assert "state=open" in calls[0]["url"] and "access_token=tok-g" in calls[0]["url"]


def test_s55_find_open_issue_gitcode_parity():
    """特征锁：gitcode 查重 URL 与分页/终止语义不变（卡 api_base 对齐原硬编码）。"""
    from src.api.issues_center import _find_open_issue_by_title
    calls, fake = _mock_transport([[{"title": "Fix login", "number": 9, "state": "open"}],
                                   []])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        n = _find_open_issue_by_title("tok-c", "acme", "web", "Fix login", provider="gitcode")
    assert n == 9
    assert calls[0]["url"].startswith("https://gitcode.com/api/v5/repos/acme/web/issues")
    assert "state=open" in calls[0]["url"] and "access_token=tok-c" in calls[0]["url"]


def test_s55_find_open_issue_dict_data_shape():
    """特征锁：{data:[...]} / list 两种响应形态解析不变（旧代码兼容形态）。"""
    from src.api.issues_center import _find_open_issue_by_title
    calls, fake = _mock_transport([{"data": [{"title": "T1", "number": 5, "state": "open"}]}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        assert _find_open_issue_by_title("t", "o", "r", "T1", provider="gitcode") == 5


def test_s55_find_open_issue_github_search_unchanged():
    """特征锁：GitHub 查重仍走 Search API 精确匹配（#344 语义；
    D36 起策略在 client 内部，接缝为传输层 mock）。"""
    from src.api.issues_center import _find_open_issue_by_title
    calls, fake = _mock_transport([{"items": [{"title": "T1", "number": 3}]}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        n = _find_open_issue_by_title("tok-h", "octo", "app", "T1", provider="github")
    assert n == 3
    assert "search/issues" in calls[0]["url"]
    assert "in%3Atitle" in calls[0]["url"] or "in:title" in calls[0]["url"]
    assert calls[0]["headers"].get("Authorization") == "token tok-h"


def test_s55_find_open_issue_token_error_raises():
    """特征锁：传输层 error 响应（token 失效）→ RuntimeError（不静默放行重复建单）。"""
    from src.api.issues_center import _find_open_issue_by_title
    calls, fake = _mock_transport([{"error": "401 unauthorized"}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        with pytest.raises(RuntimeError):
            _find_open_issue_by_title("bad", "acme", "web", "T", provider="gitcode")


# ------------------------------------------------------- S55 统一建单 helper

def test_s55_create_issue_unified_gitee(gitee_card):
    """建单统一 helper：gitee 命中卡端点 + 方言语义（github 系 assignee 单值）。"""
    from src.api.issues_center import _create_issue_unified
    calls, fake = _mock_transport([{"number": 77}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        resp = _create_issue_unified("tok-g", "gitee", "acme", "web",
                                     "T1", "desc", assignee="bob")
    assert resp.get("number") == 77
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://gitee.com/api/v5/repos/acme/web/issues?access_token=tok-g"


def test_s55_create_issue_unified_github_parity():
    """特征锁：github 走 client（token 头 + JSON + assignees 语义由 client 方言层承载）。"""
    from src.api.issues_center import _create_issue_unified
    calls, fake = _mock_transport([{"number": 12}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        resp = _create_issue_unified("tok-h", "github", "octo", "app",
                                     "T1", "desc", assignee="alice")
    assert resp.get("number") == 12
    assert calls[0]["url"] == "https://api.github.com/repos/octo/app/issues"
    assert calls[0]["headers"].get("Authorization") == "token tok-h"
