"""接管派发的内部基址选择（真机事故回归，2026-11-10）。

事故：finish 时 `_internal_base_url()` 探活 0.8s 超时（同进程正持 db_lock）→ 全部失败 →
回退到 config 的 worker_connect_url（http://localhost:3000，本机根本没人监听）→
`[Errno 111] Connection refused` → handoff_seq=0，需求停在本节点没派给下一 agent。

修法：
  ① 派发目标一律取 `_internal_base_url()`（本机探活地址）；调用方 base_url 来自
     `request.base_url`（Host 头可控），不得用于内部派发（SSRF/凭据外发）；
  ② 探活超时放宽到 2.5s；本机任务探活全失败也只回退本机地址，绝不回退公网/配置域名。
"""
import pytest

from src.domains.runtime.engine import takeover as svc


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_cache():
    svc._INTERNAL_BASE_CACHE = ""
    yield
    svc._INTERNAL_BASE_CACHE = ""


def test_dispatch_ignores_caller_base_url_and_uses_internal(monkeypatch):
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        calls["headers"] = kw.get("headers")
        calls["json"] = kw.get("json")
        return _Resp(200, {"ok": True, "response": {"seq": 4242}})

    monkeypatch.setattr("httpx.post", _post)
    monkeypatch.setattr(svc, "_internal_base_url", lambda *a, **kw: "http://127.0.0.1:8000")
    # 本用例验证"忽略调用方 base_url、只走内部基址"：显式清空服务映射，
    # 否则部署配置里的 server_base_url 会先命中（见 test_dispatch_prefers_a2a_service_mapping）。
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base", lambda *a, **kw: "")
    svc._INTERNAL_BASE_CACHE = ""
    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="7", next_agent="issue",
                             context="由用户完成 vod", runtime_id=0,
                             auth_headers={"private-token": "grape_x"},
                             base_url="http://attacker.example/")
    assert res == {"ok": True, "seq": 4242, "status": 200}
    assert calls["url"] == "http://127.0.0.1:8000/api/a2a", "内部派发不得使用 Host 头可控的 base_url"
    assert calls["headers"]["private-token"] == "grape_x"
    assert calls["json"]["agent"] == "issue"
    assert calls["json"]["payload"]["issue_number"] == "7"


def test_local_probe_failure_never_falls_back_to_public_url(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("probe failed")

    monkeypatch.setattr("httpx.get", _boom)
    monkeypatch.setattr("src.prompt_builder._resolve_server_url",
                        lambda *a, **kw: "http://localhost:3000")
    base = svc._internal_base_url(0)
    assert base == "http://127.0.0.1:8000", "探活失败也必须回退本机既定端口，不能回退 localhost:3000"


def test_probe_picks_reachable_candidate(monkeypatch):
    seen = []

    def _get(url, **kw):
        seen.append(url)
        if ":8000/" in url:
            return _Resp(200)
        raise OSError("refused")

    monkeypatch.setattr("httpx.get", _get)
    assert svc._internal_base_url(0) == "http://127.0.0.1:8000"
    # 缓存生效：第二次数不再探活
    before = len(seen)
    assert svc._internal_base_url(0) == "http://127.0.0.1:8000"
    assert len(seen) == before

def test_dispatch_prefers_a2a_service_mapping(monkeypatch):
    """四进程拓扑：/api/a2a 在 event 角色，派发必须走服务映射而不是本角色端口。#19"""
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        return _Resp(200, {"ok": True, "response": {"seq": 19}})

    monkeypatch.setattr("httpx.post", _post)
    monkeypatch.setattr(svc, "_internal_base_url", lambda *a, **kw: "http://127.0.0.1:38081")
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base",
                        lambda *a, **kw: "http://127.0.0.1:38082")
    svc._INTERNAL_BASE_CACHE = ""
    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="62", next_agent="qa",
                             context="由用户完成 coding", runtime_id=0, auth_headers={})
    assert res == {"ok": True, "seq": 19, "status": 200}
    assert calls["url"] == "http://127.0.0.1:38082/api/a2a", \
        "A2A 派发必须按服务映射取址，不能落在本角色端口（mgmt 38081 只有 GET，会 405）"


def test_dispatch_falls_back_to_internal_when_mapping_empty(monkeypatch):
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        return _Resp(200, {"ok": True, "response": {"seq": 20}})

    monkeypatch.setattr("httpx.post", _post)
    monkeypatch.setattr(svc, "_internal_base_url", lambda *a, **kw: "http://127.0.0.1:8000")
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base", lambda *a, **kw: "")
    svc._INTERNAL_BASE_CACHE = ""
    svc._dispatch_next(repo_key="gitcode:o/r", issue_number="7", next_agent="qa",
                       context="x", runtime_id=0, auth_headers={})
    assert calls["url"] == "http://127.0.0.1:8000/api/a2a"
