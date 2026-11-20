"""Issue #2084：服务端上游超时与 CLI 客户端 30s 错开（GRAPE_UPSTREAM_TIMEOUT_SECONDS）。

背景（eval-F10「客户端先断」）：此前服务端上游 timeout=30 与 CLI 客户端 30s 同值竞速——
上游慢时客户端先自行断开，收到的是本地超时而非服务端可判定的 504 uncertain，
Agent 无法区分「已落地/未落地」。错开后（服务端默认 25s < 客户端 30s）服务端先返回
504 + uncertain + request_id + 回读指引，写超时语义（#1940）对调用方可判定。

公开 seam：
- `upstream_timeout_seconds()`：env 解析（默认 25，非法/非正值回落默认）；
- `repo_manager._req` / `_req_query` / `_github_req`、`vcs.api_client.VcsApiClient._do_request`
  四个上游 client 实际下发的 timeout 均取该配置（替换写死 30）。
"""
import pytest

from src.domains.mgmt.user import repo_manager


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", raising=False)


def _capture_urlopen(monkeypatch, payload='{"ok": 1}'):
    """Mock urllib.request.urlopen：捕获 timeout 实参，返回固定 JSON。"""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload.encode()

    def _fake(req, timeout=None, **kw):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    return captured


# ===================== Slice A：配置解析（默认 25s < CLI 30s） =====================

def test_default_upstream_timeout_is_25_and_below_cli_30():
    """默认 25s，严格小于 CLI 客户端 30s（「客户端先断」错开验收项）。"""
    from src.domains.mgmt.user.repo_manager import upstream_timeout_seconds
    v = upstream_timeout_seconds()
    assert v == 25.0
    assert v < 30.0


def test_env_overrides_upstream_timeout(monkeypatch):
    """GRAPE_UPSTREAM_TIMEOUT_SECONDS 生效（运行时读取，改环境即时生效）。"""
    from src.domains.mgmt.user.repo_manager import upstream_timeout_seconds
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "37")
    assert upstream_timeout_seconds() == 37.0
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "12.5")
    assert upstream_timeout_seconds() == 12.5


def test_invalid_env_falls_back_to_default(monkeypatch):
    """非法值/非正值回落默认 25（不因配置错误把上游客户端打瘫）。"""
    from src.domains.mgmt.user.repo_manager import upstream_timeout_seconds
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "abc")
    assert upstream_timeout_seconds() == 25.0
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "0")
    assert upstream_timeout_seconds() == 25.0
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "-5")
    assert upstream_timeout_seconds() == 25.0


# ===================== Slice B：四个上游 client 实际下发配置值 =====================

def test_req_uses_configured_timeout(monkeypatch):
    """_req（gitcode 通道，vcs provider gitcode.request 底层）下发配置超时。"""
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["timeout"] = timeout
        class _R:
            status_code = 200
            text = '{"ok": 1}'
            def json(self):
                return {"ok": 1}
        return _R()

    monkeypatch.setattr(repo_manager._requests, "post", _fake_post)
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "17")
    repo_manager._req("POST", "https://gitcode.com/api/v5/repos/o/hello/issues/1/comments",
                      "tok", {"body": "x"})
    assert captured["timeout"] == 17.0


def test_req_query_uses_configured_timeout(monkeypatch):
    """_req_query（gitcode 定制接口，access_token query 凭据）urlopen 下发配置超时。"""
    captured = _capture_urlopen(monkeypatch)
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "19")
    repo_manager._req_query("GET", "/user/invite", "tok", {"page": 1})
    assert captured["timeout"] == 19.0


def test_github_req_uses_configured_timeout(monkeypatch):
    """_github_req（github 通道，vcs provider github.request 底层）urlopen 下发配置超时。"""
    captured = _capture_urlopen(monkeypatch)
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "23")
    repo_manager._github_req("POST", "https://api.github.com/repos/o/hello/issues/1/comments",
                             "tok", {"body": "x"})
    assert captured["timeout"] == 23.0


def test_vcs_api_client_uses_configured_timeout(monkeypatch):
    """VcsApiClient._do_request（close_issue/merge_pr 等 vcs 调用链）下发配置超时。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient
    captured = _capture_urlopen(monkeypatch)
    monkeypatch.setenv("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "21")
    client = VcsApiClient("gitcode", "tok", "o", "hello")
    client._request("GET", "/repos/o/hello/issues/1")
    assert captured["timeout"] == 21.0


def test_all_write_paths_default_below_30(monkeypatch):
    """未配置时四个 client 全部以默认 25s 下发（不再有写死 30 与客户端同值竞速）。"""
    from src.domains.runtime.vcs.api_client import VcsApiClient
    cap_requests = {}
    cap_urlopen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def _fake_post(url, json=None, headers=None, timeout=None):
        cap_requests["timeout"] = timeout
        class _R:
            status_code = 200
            text = '{"ok": 1}'
            def json(self):
                return {"ok": 1}
        return _R()

    monkeypatch.setattr(repo_manager._requests, "post", _fake_post)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, **kw: (cap_urlopen.update(timeout=timeout) or _Resp()))
    repo_manager._req("POST", "https://gitcode.com/api/v5/x", "tok", {})
    assert cap_requests["timeout"] == 25.0
    repo_manager._github_req("POST", "https://api.github.com/x", "tok", {})
    assert cap_urlopen["timeout"] == 25.0
    from src.domains.runtime.vcs.api_client import VcsApiClient
    VcsApiClient("gitcode", "tok", "o", "hello")._request("POST", "/x", {})
    assert cap_urlopen["timeout"] == 25.0
