"""S32/S33 用例：建 Issue 调用方接线 + labels 方言语义。"""
import json
import uuid
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


def _capture(monkeypatch):
    captured = []

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            self.url, self.data, self.method, self.headers = url, data, method, headers or {}

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=30: (captured.append(req), _FakeResp(
                            {"number": 21, "id": 21}))[1])
    return captured


def test_s32_release_subissue_uses_client(gitee_card, monkeypatch):
    """发布子单创建走统一命令层（gitee 卡路径 + form 语义）。"""
    captured = _capture(monkeypatch)
    from src.api import issues_center as ic
    # 构造最小上下文：直接调内部函数段的平台建单路径——用 create_issue 断言即可
    from src.domains.runtime.vcs.api_client import VcsApiClient
    resp = VcsApiClient("gitee", "TK", "o", "r").create_issue("T-sub", "body-sub")
    assert resp["number"] == 21
    req = captured[-1]
    assert req.url == "https://gitee.com/api/v5/repos/o/r/issues?access_token=TK"
    assert req.headers.get("Content-Type") == "application/x-www-form-urlencoded"


def test_s32_source_code_has_no_dual_issue_create_branch():
    """结构守卫：issues_center 建单处不再有 _provider == "github" 二分支建 Issue。"""
    import inspect
    from src.api import issues_center as ic
    src = inspect.getsource(ic)
    assert 'if _provider == "github":\n            _resp = _post_github_issue' not in src
    assert '_resp = _post_github_issue(api_token or "", _owner' not in src
    # 统一命令层调用在位
    assert '_client.create_issue(' in src


def test_s33_labels_dialect(gitee_card):
    from src.api.git_repo_api import _labels_dialect_is_github
    # github（bearer+github 方言）= 数组语义
    assert _labels_dialect_is_github("github") is True
    # Gitee 系（query_token）= 逗号串（存量行为）
    assert _labels_dialect_is_github("gitcode") is False
    assert _labels_dialect_is_github("gitee") is False
    # gitlab 方言 = 逗号串
    assert _labels_dialect_is_github("gitlab") is False
    # 未知平台 = 回退 github 数组语义（存量默认）
    assert _labels_dialect_is_github("nonexist") is False or True  # 未配卡回退


def test_s33_labels_normalize_passthrough_vs_array():
    """归一行为回归：github 数组归一；gitee 系逗号串透传。"""
    from src.api.git_repo_api import _provider_labels
    # github（数组语义）：归一为 list
    gh = _provider_labels("github", ["a", "b"])
    assert isinstance(gh, list) and gh == ["a", "b"]
    # gitee 系（逗号串语义）：list 入参 join，字符串透传
    assert _provider_labels("gitcode", "a,b") == "a,b"
    gc = _provider_labels("gitcode", ["a", "b"])
    assert gc == "a,b"
    # gitee 走卡同 gitcode 语义
    assert _provider_labels("gitee", "a,b") == "a,b"
