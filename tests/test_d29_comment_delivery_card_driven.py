"""D29（S49）用例：评论投递族卡驱动——行为级断言（评论确实投递到正确平台端点）。

全部单元测试：mock 传输层（VcsApiClient._do_request），按平台卡断言
URL/鉴权形态；台账入口 mock 捕获 provider 标签。不触碰真实平台。
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

GITLAB_CARD = {
    "name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
    "webhook_path": "/webhook/gitlab", "event_header": "X-Gitlab-Event",
    "api_base": "https://gitlab.example.com/api/v4", "api_auth": "private_token",
    "webhook_verify": "token_compare", "clone_auth": "oauth2",
}


@pytest.fixture(params=[
    pytest.param({"gitee": GITEE_CARD}, id="gitee"),
])
def gitee_card(request):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps(request.param),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


@pytest.fixture()
def gitlab_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitlab": GITLAB_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITLAB_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def _mock_transport(responses=None):
    """mock VcsApiClient._do_request，捕获 (url, method, data)。"""
    calls = []
    seq = list(responses or [])

    def _fake(url, method, data=None, headers=None):
        calls.append({"url": url, "method": method, "data": data, "headers": headers or {}})
        if seq:
            out = seq.pop(0)
            return out if isinstance(out, dict) else list(out)
        return {}

    return calls, _fake


# ------------------------------------------------------- S49 端点行为断言

def test_s49_post_comment_gitee_endpoint(gitee_card):
    """gitee 评论投递：URL=卡 api_base + v5 路径，鉴权=query_token（无 Bearer 头）。"""
    from src.api.event_pipeline import _post_issue_comment_ledgered
    calls, fake = _mock_transport([{"id": 99}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment") as ledger:
        _post_issue_comment_ledgered("acme/web", 5, "hello", "tok-g",
                                      repo_key="gitee:acme/web")
    assert len(calls) == 1
    c = calls[0]
    assert c["method"] == "POST"
    assert c["url"] == "https://gitee.com/api/v5/repos/acme/web/issues/5/comments?access_token=tok-g"
    assert "Authorization" not in c["headers"]      # query_token 系不带鉴权头
    assert c["data"] == {"body": "hello"}
    ledger.assert_called_once()
    assert ledger.call_args[0][0] == "gitee"        # provider 标签由 repo_key 前缀派生


def test_s49_post_comment_github_endpoint():
    """github 存量回归：api.github.com + token 头（逐字节保留原形态）。"""
    from src.api.event_pipeline import _post_issue_comment_ledgered
    calls, fake = _mock_transport([{"id": 7}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment") as ledger:
        _post_issue_comment_ledgered("octo/app", 3, "hi", "tok-h",
                                      repo_key="github:octo/app")
    c = calls[0]
    assert c["url"] == "https://api.github.com/repos/octo/app/issues/3/comments"
    assert c["headers"].get("Authorization") == "token tok-h"
    ledger.assert_called_once()
    assert ledger.call_args[0][0] == "github"


def test_s49_post_comment_gitcode_endpoint():
    """gitcode 存量回归：gitcode v5 + access_token 查询参数（D18 语义，非 Bearer）。"""
    from src.api.event_pipeline import _post_issue_comment_ledgered
    calls, fake = _mock_transport([{"id": 8}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment") as ledger:
        _post_issue_comment_ledgered("acme/web", 6, "hi", "tok-c",
                                      repo_key="gitcode:acme/web")
    c = calls[0]
    assert c["url"] == "https://gitcode.com/api/v5/repos/acme/web/issues/6/comments?access_token=tok-c"
    assert "Authorization" not in c["headers"]
    assert ledger.call_args[0][0] == "gitcode"


def test_s49_pr_comment_dialect_paths(gitee_card, gitlab_card):
    """PR 评论路径按卡派生：gitee→pulls；github→issues；gitlab→merge_requests notes。"""
    from src.api.event_pipeline import _post_result_to_pr

    def _set_cards(cfg):
        with db_lock:
            db.cursor().execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
                (json.dumps(cfg),))
            db.commit()
        invalidate_platform_card_cache()

    # gitee：query_token 系 → /pulls/{n}/comments（显式恢复 gitee 卡——gitlab fixture 已覆盖 meta）
    _set_cards({"gitee": GITEE_CARD})
    calls, fake = _mock_transport([{"id": 1}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment"):
        _post_result_to_pr("acme/web", "5", "plain conclusion text", "tok-g",
                           "vod", repo_key="gitee:acme/web")
    assert calls[0]["url"] == "https://gitee.com/api/v5/repos/acme/web/pulls/5/comments?access_token=tok-g"

    # github：PR 共用 issue 评论 API
    calls, fake = _mock_transport([{"id": 1}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment"):
        _post_result_to_pr("octo/app", "5", "plain conclusion text", "tok-h",
                           "vod", repo_key="github:octo/app")
    assert calls[0]["url"] == "https://api.github.com/repos/octo/app/issues/5/comments"

    # gitlab：merge_requests notes（gitee 分支已覆盖 meta，显式恢复 gitlab 卡）
    _set_cards({"gitlab": GITLAB_CARD})
    calls, fake = _mock_transport([{"id": 1}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment"):
        _post_result_to_pr("acme/web", "5", "plain conclusion text", "tok-l",
                           "vod", repo_key="gitlab:acme/web")
    assert calls[0]["url"] == "https://gitlab.example.com/api/v4/projects/acme%2Fweb/merge_requests/5/notes"
    assert calls[0]["headers"].get("PRIVATE-TOKEN") == "tok-l"


def test_s49_list_comments_card(gitee_card):
    """评论列表拉取：GET 卡端点 + per_page；list 透传。"""
    from src.api.event_pipeline import _list_issue_comments_card
    calls, fake = _mock_transport([[{"body": "[VOD Agent] x"}]])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake):
        out = _list_issue_comments_card("gitee:acme/web", "acme/web", "5", "tok-g")
    assert out == [{"body": "[VOD Agent] x"}]
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].startswith("https://gitee.com/api/v5/repos/acme/web/issues/5/comments")
    assert "access_token=tok-g" in calls[0]["url"]


def test_s49_post_result_to_issue_dedup_skip(gitee_card):
    """已有 agent 评论 → 只做 GET 查重、不发 POST（方案 B 去重保持）。"""
    from src.api.event_pipeline import _post_result_to_issue
    calls, fake = _mock_transport([[{"body": "agent 已经发过 [vod Agent] 结论"}]])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment"):
        _post_result_to_issue("acme/web", "5", "new conclusion text", "tok-g",
                              "vod", repo_key="gitee:acme/web")
    methods = [c["method"] for c in calls]
    assert methods == ["GET"]                     # 无 POST
    assert all("/issues/5/comments" in c["url"] for c in calls)


def test_s49_post_result_to_issue_posts_when_no_agent_comment(gitee_card):
    """无 agent 评论 → GET 后 POST 到同一卡端点，body 带 agent 前缀与 marker。"""
    from src.api.event_pipeline import _post_result_to_issue
    calls, fake = _mock_transport([[], {"id": 42}])
    with patch("src.domains.runtime.vcs.api_client.VcsApiClient._do_request", side_effect=fake), \
         patch("src.api.event_pipeline._ledger_record_comment") as ledger:
        _post_result_to_issue("acme/web", "5", "my conclusion", "tok-g",
                              "vod", repo_key="gitee:acme/web")
    assert [c["method"] for c in calls] == ["GET", "POST"]
    assert calls[1]["data"]["body"].startswith("[VOD Agent] my conclusion")
    assert "<!-- grape-auto-comment -->" in calls[1]["data"]["body"]
    ledger.assert_called_once()


# ------------------------------------------------------- S49 派生 helper

def test_s49_provider_of_repo_key():
    from src.api.event_pipeline import _provider_of_repo_key
    assert _provider_of_repo_key("gitee:acme/web") == "gitee"
    assert _provider_of_repo_key("github:octo/app") == "github"
    assert _provider_of_repo_key("acme/web") == "gitcode"   # 无前缀=存量默认
    assert _provider_of_repo_key("") == "gitcode"
