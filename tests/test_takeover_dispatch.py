"""接管派发的内部基址选择（真机事故回归，2026-11-10）。

事故：finish 时 `_internal_base_url()` 探活 0.8s 超时（同进程正持 db_lock）→ 全部失败 →
回退到 config 的 worker_connect_url（http://localhost:3000，本机根本没人监听）→
`[Errno 111] Connection refused` → handoff_seq=0，需求停在本节点没派给下一 agent。

修法：
  ① 路由层直接传 `request.base_url`（本次请求真实到达的地址），不靠猜；
  ② 探活超时放宽到 2.5s；本机任务探活全失败也只回退本机地址，绝不回退公网/配置域名。
"""
import pytest

from src.runtime import takeover as svc


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


def test_dispatch_uses_explicit_base_url(monkeypatch):
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        calls["headers"] = kw.get("headers")
        calls["json"] = kw.get("json")
        return _Resp(200, {"ok": True, "response": {"seq": 4242}})

    monkeypatch.setattr("httpx.post", _post)
    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="7", next_agent="issue",
                             context="由用户完成 vod", runtime_id=0,
                             auth_headers={"private-token": "grape_x"},
                             base_url="http://127.0.0.1:8123/")
    assert res == {"ok": True, "seq": 4242, "status": 200}
    assert calls["url"] == "http://127.0.0.1:8123/api/a2a"
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