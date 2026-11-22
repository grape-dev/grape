"""S29/D19 用例：分支列表命令卡驱动 + 全局 bot 巡检平台清单自动扩展。"""
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


def test_s29_list_branches_card_driven(gitee_card, monkeypatch):
    from src.domains.runtime.vcs.api_client import VcsApiClient
    seen = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.method, self.headers = url, method, headers or {}

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: _FakeResp([{"name": "main", "protected": True}]))
    c = VcsApiClient("gitee", "TK", "o", "r")
    branches = c.list_branches()
    assert branches == [{"name": "main", "protected": True}]
    # URL 断言：卡 api_base + query_token（通过捕获 lambda 侧记录）
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: (captured.append(req), _FakeResp(
                            [{"name": "main"}]))[1])
    c.list_branches()
    assert captured[-1].url.startswith("https://gitee.com/api/v5/repos/o/r/branches?per_page=100")
    assert "access_token=TK" in captured[-1].url


def test_s29_bot_health_platforms_auto_extended(gitee_card):
    """config-only 平台配了全局 bot token 自动纳入巡检；未配不纳入。"""
    from src.domains.runtime.engine.bot_token_health import _verify_bot_token, _verify_global_bot_token
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('global_bot_gitee_token', 'gbt_gitee')")
        db.commit()
    try:
        # 单平台验证走统一 token 来源链（mock verify_token 避免真实网络）
        with patch("src.kernel.token_resolver.create_provider") as mp:
            class _FakeVCS:
                def verify_token(self, token):
                    assert token == "gbt_gitee"
                    return {"login": "bot"}
            mp.return_value = _FakeVCS()
            res = _verify_bot_token("gitee")
            assert res["healthy"] is True and res["error_type"] == ""
        # 巡检清单自动含 gitee（mock 内置两平台避免网络）
        with patch("src.domains.runtime.engine.bot_token_health._verify_bot_token",
                   side_effect=lambda p: {"healthy": True, "error": "", "error_type": ""}):
            out = _verify_global_bot_token()
            assert {"gitcode", "github", "gitee"} <= set(out["platforms"])
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='global_bot_gitee_token'")
            db.commit()
    # 未配 token 的 config-only 平台不进巡检。
    # 注意：基集 = 提供商模块注册表（gitcode/github/gitee/gitlab 均已注册），
    # 所以「只靠 meta token 才纳入」这条只能用未注册的假平台验证。
    from src.domains.runtime.engine import bot_token_health as _bth
    with patch("src.domains.runtime.vcs.platform_card.list_vcs_platforms",
               return_value=["fakecardonly"]), \
         patch("src.domains.mgmt.public.get_global_bot_token_for", return_value=""), \
         patch.object(_bth, "_verify_bot_token",
                      side_effect=lambda p: {"healthy": True, "error": "", "error_type": ""}):
        out = _bth._verify_global_bot_token()
        assert "fakecardonly" not in out["platforms"]


def test_s29_scenes_branch_list_uses_client_for_gitee(gitee_card, monkeypatch):
    """scenes 分支查询：gitee 仓库走统一命令层（卡 endpoint），不再错配 gitcode API。"""
    captured = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.method, self.headers = url, method, headers or {}

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: (captured.append(req), _FakeResp(
                            [{"name": "main"}]))[1])
    from src.domains.mgmt.routes.scenes import list_repo_branches
    with patch("src.domains.mgmt.user.db.get_repo_token_with_refresh", return_value="TK"):
        out = list_repo_branches("o", "r", repo_key="gitee:o/r")
    assert out["branches"][0]["name"] == "main"
    assert captured[-1].url.startswith("https://gitee.com/api/v5/repos/o/r/branches")


def test_s29_list_branches_truncation_signal(gitee_card, monkeypatch):
    """#3758 R1：分页达到上限仍满页时返回截断信号，调用方不得当作「不存在」。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url = url

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: _FakeResp(
                            [{"name": "a"}, {"name": "b"}]))
    c = VcsApiClient("gitee", "TK", "o", "r")
    branches, truncated = c.list_branches(
        max_pages=1, per_page=2, with_truncation=True)
    assert [b["name"] for b in branches] == ["a", "b"]
    assert truncated is True


def test_s29_get_default_branch_github_dialect(gitee_card, monkeypatch):
    """#3758 R3：默认分支读取走方言正确的公开方法（github 方言 /repos/...）。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient
    captured = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url = url

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: (captured.append(req), _FakeResp(
                            {"default_branch": "main"}))[1])
    c = VcsApiClient("gitee", "TK", "o", "r")
    assert c.get_default_branch() == "main"
    assert captured[-1].url.startswith("https://gitee.com/api/v5/repos/o/r?")
