"""接管派发的内部基址选择（真机事故回归，2026-11-10；#3910 探活判据）。

事故 1（2026-11-10）：finish 时 `_internal_base_url()` 探活 0.8s 超时（同进程正持 db_lock）
→ 全部失败 → 回退到 config 的 worker_connect_url（http://localhost:3000，本机没人监听）
→ `[Errno 111] Connection refused` → handoff_seq=0。

事故 2（#3910）：多服务共存部署下，辅助进程 `/api/health`=200 但没有 `/api/a2a` 路由，
health-only 判据命中辅助端口 → 派发 POST /api/a2a 返回 405 → 静默丢派发，外层仍 ok:true。

修法：
  ① 派发基址仍忽略调用方 base_url（Host 头可控，SSRF/凭据外发）；
  ② 显式 `resolve_a2a_base_with_source()` 的显式配置/网关前缀可信任；
     `runtime_server_url_fallback`（四进程下常是本进程 mgmt 端口）不可信任，
     回退路径做「/api/health 可达 + POST /api/a2a 路由可用」双重校验；
  ③ 进程级缓存只存校验通过的基址；405 时清缓存并在候选集内重选重试一次。
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
    svc._A2A_DISPATCH_BASE_CACHE = ""
    svc._BUSINESS_BASE_CACHE = ""
    yield
    svc._A2A_DISPATCH_BASE_CACHE = ""
    svc._BUSINESS_BASE_CACHE = ""


def _patch_source(monkeypatch, base, source="runtime_server_url_fallback"):
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base_with_source",
                        lambda *a, **kw: (base, source))


def test_dispatch_ignores_caller_base_url_and_uses_internal(monkeypatch):
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        calls["headers"] = kw.get("headers")
        calls["json"] = kw.get("json")
        return _Resp(200, {"ok": True, "response": {"seq": 4242}})

    monkeypatch.setattr("httpx.post", _post)
    # 本用例验证「忽略调用方 base_url、只走内部基址」：清空服务映射，
    # 否则部署配置里的 server_base_url 会先命中（见 test_dispatch_skips_health_only_role）。
    _patch_source(monkeypatch, "")
    monkeypatch.setattr(svc, "_a2a_internal_base", lambda *a, **kw: "http://127.0.0.1:8000")
    svc._A2A_DISPATCH_BASE_CACHE = ""
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


def test_dispatch_uses_trusted_explicit_a2a_base_without_probe(monkeypatch):
    """显式 A2A 配置/网关前缀可信任：直接派发，不额外探活。"""
    calls = {"post": [], "get": []}

    def _post(url, **kw):
        calls["post"].append(url)
        return _Resp(200, {"ok": True, "response": {"seq": 19}})

    def _get(url, **kw):
        calls["get"].append(url)
        return _Resp(200)

    monkeypatch.setattr("httpx.post", _post)
    monkeypatch.setattr("httpx.get", _get)
    _patch_source(monkeypatch, "https://gw.example.com/a2a", source="gateway_prefix_a2a")
    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="62", next_agent="qa",
                             context="由用户完成 coding", runtime_id=0, auth_headers={})
    assert res == {"ok": True, "seq": 19, "status": 200}
    assert calls["post"] == ["https://gw.example.com/a2a/api/a2a"]
    assert calls["get"] == [], "显式配置/网关前缀不得触发可达性探测"


def test_dispatch_rejects_nondispatchable_server_url_fallback(monkeypatch, caplog):
    """真实回退到 server_url（本进程 mgmt 端口）不可派发时：warning + 选正确 A2A 端口并缓存。"""
    _patch_source(monkeypatch, "http://127.0.0.1:38081")  # 四进程下 mgmt 的 server_url
    monkeypatch.setattr(svc, "_dispatch_base_candidates",
                        lambda: ["http://127.0.0.1:38081", "http://127.0.0.1:38082"])
    posted = []

    def _get(url, **kw):
        return _Resp(200)  # 两个端口 health 都 200

    def _post(url, **kw):
        posted.append(url)
        if ":38081/" in url:   # mgmt：只有 GET，没有 /api/a2a 路由
            return _Resp(405)
        if not kw.get("json"):  # 空 payload = 探活：端点存在（缺参 400）
            return _Resp(400)
        return _Resp(200, {"ok": True, "response": {"seq": 66}})

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="9", next_agent="qa",
                             context="x", runtime_id=0, auth_headers={})
    assert res == {"ok": True, "seq": 66, "status": 200}, "首次派发必须命中可派发端口而非 405"
    assert posted == ["http://127.0.0.1:38081/api/a2a", "http://127.0.0.1:38082/api/a2a",
                      "http://127.0.0.1:38082/api/a2a"], \
        "不可派发的 server_url 回退必须先被校验排除，再改用 A2A 实际挂载端口"
    assert svc._A2A_DISPATCH_BASE_CACHE == "http://127.0.0.1:38082", \
        "只缓存通过双重校验的基址"
    assert any("skip a2a dispatch candidate http://127.0.0.1:38081" in r.message
               for r in caplog.records), "命中的不可派发回退基址必须输出 warning"

    # 缓存生效：第二次派发直接走正确端口，不再反复对坏端口探活/告警
    before = len(posted)
    svc._dispatch_next(repo_key="gitcode:o/r", issue_number="9", next_agent="qa",
                       context="x", runtime_id=0, auth_headers={})
    assert posted[before:] == ["http://127.0.0.1:38082/api/a2a"]


def test_dispatch_falls_back_to_internal_when_mapping_empty(monkeypatch):
    calls = {}

    def _post(url, **kw):
        calls["url"] = url
        return _Resp(200, {"ok": True, "response": {"seq": 20}})

    monkeypatch.setattr("httpx.post", _post)
    _patch_source(monkeypatch, "")
    monkeypatch.setattr(svc, "_a2a_internal_base", lambda *a, **kw: "http://127.0.0.1:8000")
    svc._A2A_DISPATCH_BASE_CACHE = ""
    svc._dispatch_next(repo_key="gitcode:o/r", issue_number="7", next_agent="qa",
                       context="x", runtime_id=0, auth_headers={})
    assert calls["url"] == "http://127.0.0.1:8000/api/a2a"


def test_select_dispatch_base_skips_health_only_aux_service(monkeypatch):
    """辅助服务 health=200 但 a2a=405 → 排除；选中真正可派发的端口并缓存。"""
    monkeypatch.setattr(svc, "_dispatch_base_candidates",
                        lambda: ["http://127.0.0.1:38081", "http://127.0.0.1:38082"])

    probe_calls = []

    def _get(url, **kw):
        probe_calls.append(("get", url))
        return _Resp(200)  # 两个端口 health 都 200

    def _post(url, **kw):
        probe_calls.append(("post", url))
        if ":38081/" in url:      # mgmt：只有 GET，没有 /api/a2a 路由
            return _Resp(405)
        return _Resp(400)         # event：端点存在（空 payload 缺参 400）

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    assert svc._a2a_internal_base(0) == "http://127.0.0.1:38082"
    assert svc._A2A_DISPATCH_BASE_CACHE == "http://127.0.0.1:38082", \
        "只缓存通过双重校验的基址，错误基址不得进入进程级缓存"

    # 缓存命中后不再探测（避免每次 finish 都多打一轮 health+post）
    before = len(probe_calls)
    assert svc._a2a_internal_base(0) == "http://127.0.0.1:38082"
    assert len(probe_calls) == before


def test_remote_server_url_fallback_is_double_checked(monkeypatch, caplog):
    """runtime_id>0：server_url 回连基址同样双重校验，通过才缓存。"""
    monkeypatch.setattr("src.kernel.prompt_builder._resolve_server_url",
                        lambda *a, **kw: "http://worker.example.com:38082")
    need = {"a2a": 400}

    def _get(url, **kw):
        return _Resp(200)

    def _post(url, **kw):
        return _Resp(need["a2a"])

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    assert svc._a2a_internal_base(7) == "http://worker.example.com:38082"
    assert svc._A2A_DISPATCH_BASE_CACHE == "http://worker.example.com:38082"

    # 不可派发：warning + 不缓存，但远程 worker 不能回退本机端口，仍最佳努力用回连基址
    svc._A2A_DISPATCH_BASE_CACHE = ""
    need["a2a"] = 405
    assert svc._a2a_internal_base(7) == "http://worker.example.com:38082"
    assert svc._A2A_DISPATCH_BASE_CACHE == "", "远程基址校验失败不得写入缓存"
    assert any("skip a2a dispatch candidate http://worker.example.com:38082" in r.message
               for r in caplog.records)


def test_rejected_candidate_not_cached_and_falls_back(monkeypatch):
    """全部候选都不可派发时回退本机端口，但不得把错误基址写入缓存。"""
    monkeypatch.setattr(svc, "_dispatch_base_candidates",
                        lambda: ["http://127.0.0.1:38084", "http://127.0.0.1:38081"])

    def _get(url, **kw):
        return _Resp(200)

    def _post(url, **kw):
        return _Resp(404)  # insight 无路由

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    base = svc._a2a_internal_base(0)
    assert base.startswith("http://127.0.0.1:")
    assert svc._A2A_DISPATCH_BASE_CACHE == "", "不可派发基址不得进入进程级缓存"


def test_dedupe_bases_keeps_only_http_absolute_urls():
    """只保留 http:// / https:// 绝对地址（``httpfoo`` 之类的伪协议不能通过）。"""
    assert svc._dedupe_bases([
        "httpfoo", "https://b/", "http://a", "http://a/", " http://c ", "",
    ]) == ["https://b", "http://a", "http://c"]


def test_dispatch_retries_with_reselected_base_on_405(monkeypatch):
    """缓存/竞态仍 405 时：清缓存、排除坏基址重选并重试一次。"""
    resolved = {"first": "http://127.0.0.1:38081", "second": "http://127.0.0.1:38082"}
    seen = []

    def _dispatch_base(runtime_id=0, exclude=None):
        exclude = {b.rstrip("/") for b in (exclude or set())}
        for key in ("first", "second"):
            b = resolved[key]
            if b.rstrip("/") not in exclude:
                return b
        return "http://127.0.0.1:38082"

    monkeypatch.setattr(svc, "_a2a_dispatch_base", _dispatch_base)

    def _post(url, **kw):
        seen.append(url)
        if ":38081/" in url:
            return _Resp(405)
        return _Resp(200, {"ok": True, "response": {"seq": 55}})

    monkeypatch.setattr("httpx.post", _post)
    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="9", next_agent="qa",
                             context="x", runtime_id=0, auth_headers={})
    assert res == {"ok": True, "seq": 55, "status": 200}
    assert seen == ["http://127.0.0.1:38081/api/a2a", "http://127.0.0.1:38082/api/a2a"], \
        "405 后必须换基址重试一次，不能对同一坏端口盲重试"
    assert svc._A2A_DISPATCH_BASE_CACHE == "", "405 必须清掉进程级缓存"

def test_local_worker_loopback_reselects_dispatchable_port(monkeypatch, caplog):
    """runtime_id>0 但回连基址是本机回环（四进程 mgmt 38081）：按本机候选扫描重选。

    回归 QA 复审缺陷：旧实现把 ``runtime_id>0`` 一律当远程 worker，探活失败后
    best-effort 返回已证 405 的 38081；本机 worker 必须像 runtime_id=0 一样重选 38082。
    """
    monkeypatch.setattr("src.kernel.prompt_builder._resolve_server_url",
                        lambda *a, **kw: "http://127.0.0.1:38081")
    monkeypatch.setattr("src.kernel.prompt_builder._runtime_is_local",
                        lambda *a, **kw: None)  # locality 未知时以回环地址为判据
    monkeypatch.setattr(svc, "_dispatch_base_candidates",
                        lambda: ["http://127.0.0.1:38081", "http://127.0.0.1:38082"])
    posted = []

    def _get(url, **kw):
        return _Resp(200)

    def _post(url, **kw):
        posted.append(url)
        if ":38081/" in url:      # mgmt：只有 GET，没有 /api/a2a 路由
            return _Resp(405)
        if not kw.get("json"):    # 空 payload = 探活：端点存在（缺参 400）
            return _Resp(400)
        return _Resp(200, {"ok": True, "response": {"seq": 7}})

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    base = svc._a2a_dispatch_base(7)
    assert base == "http://127.0.0.1:38082", \
        "本机 worker 必须重选同机可派发端口，不能 best-effort 打已证 405 的 38081"
    assert svc._A2A_DISPATCH_BASE_CACHE == "http://127.0.0.1:38082", \
        "只缓存通过双重校验的本机 worker 派发基址"
    assert posted == ["http://127.0.0.1:38081/api/a2a",
                      "http://127.0.0.1:38082/api/a2a"], \
        "先探活排除 38081，再探活 38082"
    assert any("skip a2a dispatch candidate http://127.0.0.1:38081" in r.message
               for r in caplog.records), "不可派发的本机 worker 回环基址必须输出 warning"


def test_local_worker_405_reselects_new_base(monkeypatch):
    """本机 worker 405 重选：排除坏基址后必须换到 38082，不能返回同一基址空转。"""
    monkeypatch.setattr("src.kernel.prompt_builder._resolve_server_url",
                        lambda *a, **kw: "http://127.0.0.1:38081")
    monkeypatch.setattr("src.kernel.prompt_builder._runtime_is_local",
                        lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_dispatch_base_candidates",
                        lambda: ["http://127.0.0.1:38081", "http://127.0.0.1:38082"])
    # 旧缓存（竞态/部署变更后已失效）指向 38081，首次派发命中 405
    svc._A2A_DISPATCH_BASE_CACHE = "http://127.0.0.1:38081"
    seen = []

    def _get(url, **kw):
        return _Resp(200)

    def _post(url, **kw):
        seen.append(url)
        if ":38081/" in url:
            return _Resp(405)
        if not kw.get("json"):
            return _Resp(400)
        return _Resp(200, {"ok": True, "response": {"seq": 88}})

    monkeypatch.setattr("httpx.get", _get)
    monkeypatch.setattr("httpx.post", _post)

    res = svc._dispatch_next(repo_key="gitcode:o/r", issue_number="9", next_agent="qa",
                             context="x", runtime_id=7, auth_headers={})
    assert res == {"ok": True, "seq": 88, "status": 200}
    assert seen == ["http://127.0.0.1:38081/api/a2a",
                    "http://127.0.0.1:38082/api/a2a",
                    "http://127.0.0.1:38082/api/a2a"], \
        "本机 worker 405 后必须排除坏基址重选（探活 38082）并重试一次"
    assert svc._A2A_DISPATCH_BASE_CACHE == "http://127.0.0.1:38082", \
        "重选成功后缓存新基址"


def test_dispatch_candidates_include_gsb_env_url_map(monkeypatch):
    """候选集覆盖 GSB ``_ENV_URL_MAP``（GRAPE_EVENT_URL 等），避免自定义端口扫不到。"""
    monkeypatch.setenv("GRAPE_EVENT_URL", "https://event.example.com:39000")
    monkeypatch.setenv("GRAPE_RUNTIME_URL", "https://runtime.example.com:39001")
    cands = svc._dispatch_base_candidates()
    assert "https://event.example.com:39000" in cands
    assert "https://runtime.example.com:39001" in cands
