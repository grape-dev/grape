"""D41（S61）用例：gitee issue 写路径方言（实弹观测）——卡能力位 issue_write_form。

实弹观测（2026-11-03，token verify/读写全链路对 example-org/example-repo）：
- 读路径（GET 列表/单条）：repo-path 形态 /repos/{o}/{r}/issues[/{ident}]
  ——与 GitCode 同，零改动；
- 建单：POST /repos/{owner}/issues + form {repo, title, body}（repo=仓名），
  非 GitCode 的 /repos/{o}/{r}/issues；
- 关单：PATCH /repos/{owner}/issues/{ident} + form {state: closed, repo, title}；
- number 是字符串 ident（"IKSQ4O"）——查重 int() 强转会炸。

解法（D36/D38 同款）：卡新能力位 issue_write_form（默认 repo_path=存量
GitCode/GitHub 形态；owner_param=gitee 形态），create/close 按能力位派生
路径与 body；number 保持平台原生形态（int 平台仍 int）。
"""
import json
import urllib.parse
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import build_platform_card, invalidate_platform_card_cache
from src.domains.runtime.vcs.api_client import VcsApiClient


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "issue_write_form": "owner_param",
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


def _client(provider, token="T", owner="acme", repo="web"):
    return VcsApiClient(provider, token, owner, repo)


def _capture(method, url, data=None, headers=None, **kw):
    return {"method": method, "url": url, "data": data, "headers": headers or {}}


# ------------------------------------------------ S61 卡能力位

def test_s61_card_issue_write_form_default_and_validation():
    card = build_platform_card("foo", {"host": "foo.com", "dialect": "github"})
    assert card.issue_write_form == "repo_path"
    with pytest.raises(Exception):
        build_platform_card("foo", {"host": "foo.com", "dialect": "github",
                                    "issue_write_form": "bogus"})
    assert build_platform_card("foo", {"host": "foo.com", "dialect": "github",
                                       "issue_write_form": "owner_param"}).issue_write_form == "owner_param"


# ------------------------------------------------ S61 建单形态（实弹形态断言）

def test_s61_create_issue_gitee_owner_param(gitee_card):
    """gitee：POST /repos/{owner}/issues + form repo=仓名（实弹观测形态）。"""
    c = _client("gitee")
    with patch.object(VcsApiClient, "_do_request", side_effect=lambda m, u, d=None, headers=None, **kw:
                      {"number": "IKSQ4O", "state": "open"}) as m_req:
        r = c.create_issue("标题", body="正文")
    assert r["number"] == "IKSQ4O"
    url = m_req.call_args.args[0].split("?")[0]
    assert url == "https://gitee.com/api/v5/repos/acme/issues"
    body = m_req.call_args.args[2]
    assert isinstance(body, str)  # form 编码
    form = dict(urllib.parse.parse_qsl(body))
    assert form["repo"] == "web"
    assert form["title"] == "标题"
    assert form["body"] == "正文"


def test_s61_create_issue_gitcode_parity():
    """gitcode（repo_path 默认）：/repos/{o}/{r}/issues + form {title, body} 不变。"""
    c = _client("gitcode")
    with patch.object(VcsApiClient, "_do_request", side_effect=lambda m, u, d=None, headers=None, **kw:
                      {"number": 7}) as m_req:
        c.create_issue("t", body="b")
    url = m_req.call_args.args[0].split("?")[0]
    assert url == "https://gitcode.com/api/v5/repos/acme/web/issues"
    body = m_req.call_args.args[2]
    assert isinstance(body, str)
    form = dict(urllib.parse.parse_qsl(body))
    assert form == {"title": "t", "body": "b"}


# ------------------------------------------------ S61 关单形态（实弹形态断言）

def test_s61_close_issue_gitee_owner_param(gitee_card):
    """gitee：PATCH /repos/{owner}/issues/{ident} + form {state=closed, repo=仓名, title}。"""
    c = _client("gitee")
    with patch.object(VcsApiClient, "_do_request", side_effect=[
        {"number": "IKSQ4O", "state": "open", "title": "老标题"},   # get_issue（title 回填）
        {"number": "IKSQ4O", "state": "closed"},                     # PATCH
    ]) as m_req:
        r = c.close_issue("IKSQ4O")
    assert r["state"] == "closed"
    patch_call = m_req.call_args_list[-1]
    url = patch_call.args[0].split("?")[0]
    assert url == "https://gitee.com/api/v5/repos/acme/issues/IKSQ4O"
    form = dict(urllib.parse.parse_qsl(patch_call.args[2]))
    assert form["state"] == "closed"
    assert form["repo"] == "web"
    assert form["title"] == "老标题"


def test_s61_close_issue_gitcode_parity():
    """gitcode：PATCH /repos/{o}/{r}/issues/{n} + {state=close, repo=owner/repo, title} 不变。"""
    c = _client("gitcode")
    with patch.object(VcsApiClient, "_do_request", side_effect=[
        {"number": 7, "state": "open", "title": "T"},
        {"number": 7, "state": "closed"},
    ]) as m_req:
        c.close_issue(7)
    patch_call = m_req.call_args_list[-1]
    assert patch_call.args[0].split("?")[0] == "https://gitcode.com/api/v5/repos/acme/web/issues/7"
    body = patch_call.args[2]
    assert body == {"state": "close", "repo": "acme/web", "title": "T"}


# ------------------------------------------------ S61 number 原生形态（ident 字符串）

def test_s61_find_issue_number_native_ident(gitee_card):
    """gitee 查重命中返回字符串 ident 原生形态（int() 强转消除，不炸）。"""
    c = _client("gitee")
    with patch.object(VcsApiClient, "_request", return_value=[
        {"number": "IKSQ4O", "title": "T", "state": "open"},
    ]):
        assert c.find_open_issue_by_title("T") == "IKSQ4O"


def test_s61_find_issue_number_int_platform_parity():
    """gitcode/github：number 仍返回 int（原生形态保持）。"""
    c = _client("gitcode")
    with patch.object(VcsApiClient, "_request", return_value=[
        {"number": 7, "title": "T", "state": "open"},
    ]):
        assert c.find_open_issue_by_title("T") == 7
