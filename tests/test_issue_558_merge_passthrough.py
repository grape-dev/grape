"""Issue #558：pulls merge 上游错误透传 + merge_method/title 参数全链路。

覆盖（Spec 验收标准）：
- 上游 405（PR 不可自动合并）→ 网关透传 405 + 完整上游 message（非 502、不截断）
- merge_method/title 透传至上游 merge 端点 body；缺省不改变存量请求（body=None）
- 上游 2xx 合并成功路径回归不变
- 代理自身异常（超时/连接失败）仍返回 502
- provider 层 _req 上游非 2xx 抛 UpstreamAPIError（status_code + 完整 message；冲突解决后与 #549 UpstreamAPIError 统一）
- VcsApiClient.merge_pr 参数透传（gitcode/github/gitlab squash 映射）
"""
import json as _json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.user.repo_manager import UpstreamAPIError

UID = 98755
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_558"

_LONG_MSG = "x" * 1200  # > 上游旧截断上限 500，验证不截断


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "merge_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s558",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _merge(client, number=9, **payload):
    return client.put(f"/api/git-repo/{OWNER}/{REPO}/pulls/{number}/merge", json=payload or None)


def test_upstream_405_passthrough(monkeypatch, _client):
    """上游 405（不可自动合并）→ 透传 405 + 完整 message，非 502、不截断。"""
    def _fake_req(method, url, token="", body=None):
        raise UpstreamAPIError(method, url, 405, _LONG_MSG)
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _merge(_client)
    assert r.status_code == 405
    detail = r.json()["detail"]
    assert detail["code"] == 405
    assert detail["message"] == _LONG_MSG  # 完整全文，未截断


def test_upstream_generic_error_keeps_502(monkeypatch, _client):
    """代理自身异常（超时/连接失败类 RuntimeError）→ 仍 502（网关自身故障语义）。"""
    def _fake_req(method, url, token="", body=None):
        raise RuntimeError("Cannot reach GitCode API: connection timed out")
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _merge(_client)
    assert r.status_code == 502


def test_merge_params_passthrough(monkeypatch, _client):
    """merge_method/title 透传至上游 merge 端点 body。"""
    recorded = {}

    def _fake_req(method, url, token="", body=None):
        recorded.update({f"{method}:{url}": {"method": method, "url": url, "body": body}})
        return {"merged": True, "sha": "abc123"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _merge(_client, merge_method="squash", title="chore: squash merge")
    assert r.status_code == 200
    # Issue #752：merge 前后各有一次门禁 GET /pulls/9/files 扫描，「最后一次调用」
    # 不再是 merge PUT——改为按键断言 merge PUT 已按透传参数发出。
    _puts = [v for (k, v) in recorded.items()
             if k.startswith("PUT:") and k.split("?")[0].endswith("/pulls/9/merge")]
    assert len(_puts) == 1
    assert _puts[0]["body"] == {"merge_method": "squash", "title": "chore: squash merge"}


def test_merge_default_keeps_legacy_body(monkeypatch, _client):
    """缺省（无 body / 全空字段）→ 上游 body=None，存量行为不变。"""
    recorded = {}

    def _fake_req(method, url, token="", body=None):
        recorded.update({"method": method, "url": url, "body": body})
        return {"merged": True, "sha": "abc123"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    assert _merge(_client).status_code == 200  # 无 JSON body
    assert recorded["body"] is None
    assert _merge(_client, merge_method="", title="").status_code == 200  # 全空字段
    assert recorded["body"] is None


def test_merge_success_regression(monkeypatch, _client):
    """上游 2xx 合并成功路径回归：merged/sha 返回不变。"""
    def _fake_req(method, url, token="", body=None):
        return {"merged": True, "sha": "deadbeef"}
    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    r = _merge(_client)
    assert r.status_code == 200
    assert r.json() == {"merged": True, "sha": "deadbeef"}


# ===================== provider 层 _req 结构化异常 =====================

class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def test_req_raises_vcs_upstream_error_with_full_body(monkeypatch):
    """_req 上游非 2xx → UpstreamAPIError(status_code, 完整 message)。"""
    import src.user.repo_manager as m
    monkeypatch.setattr(m._requests, "put",
                        lambda *a, **k: _FakeResp(405, _LONG_MSG))
    with pytest.raises(UpstreamAPIError) as ei:
        m._req("PUT", "https://gitcode.com/api/v5/repos/o/hello/pulls/9/merge", BOT, {})
    assert ei.value.status_code == 405
    assert ei.value.message == _LONG_MSG  # 不截断
    assert "(405)" in str(ei.value)  # _extract_http_code 兼容
    # 兼容：仍是 RuntimeError（存量 except RuntimeError 调用方不破坏）
    assert isinstance(ei.value, RuntimeError)


# ===================== VcsApiClient.merge_pr 参数透传 =====================

def _make_client(provider="gitcode"):
    from src.vcs.api_client import VcsApiClient
    return VcsApiClient(provider, BOT, OWNER, REPO)


def test_client_merge_pr_params_gitcode(monkeypatch):
    c = _make_client("gitcode")
    recorded = {}

    def _fake(method, path, data=None):
        recorded.update({"method": method, "path": path, "data": data})
        return {"merged": True, "sha": "s1"}

    monkeypatch.setattr(c, "_gitcode_request", _fake)
    resp = c.merge_pr(9, merge_method="rebase", title="t1")
    assert resp["merged"] is True
    assert recorded["path"].endswith("/pulls/9/merge")
    assert recorded["data"] == {"merge_method": "rebase", "title": "t1"}


def test_client_merge_pr_default_keeps_empty_body(monkeypatch):
    c = _make_client("gitcode")
    recorded = {}

    def _fake(method, path, data=None):
        recorded["data"] = data
        return {"merged": True, "sha": "s2"}

    monkeypatch.setattr(c, "_gitcode_request", _fake)
    c.merge_pr(9)
    assert recorded["data"] == {}  # 存量行为（空 dict）不变


def test_client_merge_pr_squash_gitlab(monkeypatch):
    c = _make_client("gitlab")
    recorded = {}

    def _fake(method, path, data=None):
        recorded["data"] = data
        return {"merged": True, "sha": "s3"}

    monkeypatch.setattr(c, "_gitlab_request", _fake)
    c.merge_pr(9, merge_method="squash")
    assert recorded["data"] == {"squash": True}
    c.merge_pr(9)
    assert recorded["data"] == {}


def test_client_do_request_error_keeps_status(monkeypatch):
    """_do_request HTTPError → 错误 dict 含 status_code 与完整 detail。"""

    class _Err(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 405, "Method Not Allowed", None, None)

        def read(self):
            return _LONG_MSG.encode()

    def _fake_urlopen(req, timeout=30):
        raise _Err()

    monkeypatch.setattr("src.vcs.api_client.urllib.request.urlopen", _fake_urlopen)
    from src.vcs.api_client import VcsApiClient
    resp = VcsApiClient._do_request("https://x", "PUT", {})
    assert resp["error"] == "HTTP 405"
    assert resp["status_code"] == 405
    assert resp["detail"] == _LONG_MSG
