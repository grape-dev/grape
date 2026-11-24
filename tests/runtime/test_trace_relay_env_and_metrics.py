"""trace 中继两个坑的回归用例（2026-11-23 生产实锤）。

坑 1：OTLP_RELAY_TARGET 曾在模块导入时被快照成常量，运维改了 env 但没重启进程时
      端点一直回 503「trace relay disabled」，nginx 日志里只有一行 503，分不清
      「没配」还是「配了没重启」。现改为按请求现读 env。
坑 2：同名 Counter 在 runtime.py 里被定义两次，第二次重复注册抛 ValueError 被
      except 吞掉，把 _relay_total 置成 None —— 中继一直转发成功，指标却只有
      HELP/TYPE 零样本。现只保留一处定义且失败要留痕。

这两个用例都不依赖外部 LGTM：上游用 127.0.0.1 的临时 HTTP server 模拟。
"""
import http.server
import threading

import pytest
from fastapi import FastAPI
from prometheus_client import generate_latest
from starlette.testclient import TestClient


class _Upstream(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"\x00\x00"
    requests: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        _Upstream.requests.append((self.path, self.rfile.read(n),
                                   self.headers.get("content-type")))
        self.send_response(_Upstream.status)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", str(len(_Upstream.body)))
        self.end_headers()
        self.wfile.write(_Upstream.body)

    def log_message(self, *a):  # 静默
        pass


@pytest.fixture()
def upstream():
    _Upstream.requests = []
    _Upstream.status = 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _app():
    from src.domains.runtime.routes import runtime as rt

    app = FastAPI()
    app.include_router(rt.router)
    app.dependency_overrides[rt._require_credential] = lambda: 1
    return app


def test_relay_target_env_is_read_per_request(monkeypatch, upstream):
    """import 之后改 env 立刻生效：不再需要重启进程，也不会残留 503。"""
    from src.domains.runtime.routes import runtime as rt

    monkeypatch.delenv("OTLP_RELAY_TARGET", raising=False)
    assert rt._relay_target() == ""

    monkeypatch.setenv("OTLP_RELAY_TARGET", f"http://127.0.0.1:{upstream}/")
    client = TestClient(_app())
    r = client.post("/api/runtime/traces", content=b"\x08\x01",
                    headers={"content-type": "application/x-protobuf"})

    assert r.status_code == 200
    assert _Upstream.requests[-1][0] == "/v1/traces"  # 尾部斜杠已规整
    assert _Upstream.requests[-1][1] == b"\x08\x01"   # body 原样透传
    assert _Upstream.requests[-1][2] == "application/x-protobuf"


def test_relay_counter_has_samples_after_forward(monkeypatch, upstream):
    """成功转发必须落在 grape_trace_relay_requests_total 上（坑 2 的判据）。"""
    from src.domains.insight import public as obs_public
    from src.domains.runtime.routes import runtime as rt

    assert rt._relay_total is not None, (
        "Counter 不可用（历史上正是这里被重复注册的 except 置成 None）")

    monkeypatch.setenv("OTLP_RELAY_TARGET", f"http://127.0.0.1:{upstream}")
    client = TestClient(_app())
    assert client.post("/api/runtime/traces", content=b"\x08\x01",
                       headers={"content-type": "application/x-protobuf"}).status_code == 200

    text = generate_latest(obs_public.prometheus_registry()).decode()
    assert 'grape_trace_relay_requests_total{status="200"}' in text


def test_relay_unreachable_counts_and_returns_502(monkeypatch):
    """上游不可达：502 + status="relay_unreachable" 计数（与 503 区分得开）。"""
    from src.domains.insight import public as obs_public
    from src.domains.runtime.routes import runtime as rt

    monkeypatch.setenv("OTLP_RELAY_TARGET", "http://127.0.0.1:9")  # 不可达
    client = TestClient(_app())
    r = client.post("/api/runtime/traces", content=b"\x08\x01",
                    headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 502

    text = generate_latest(obs_public.prometheus_registry()).decode()
    assert 'grape_trace_relay_requests_total{status="relay_unreachable"}' in text


def test_disabled_503_warns_but_is_throttled(monkeypatch):
    """未配置时 503 有告警，但 10 分钟内不重复刷屏。"""
    from src.domains.runtime.routes import runtime as rt

    calls = []
    monkeypatch.setattr(rt.log, "warning", lambda msg, *a, **kw: calls.append(str(msg)))
    monkeypatch.setattr(rt, "_relay_warn_ts", 0.0, raising=True)
    monkeypatch.delenv("OTLP_RELAY_TARGET", raising=False)

    client = TestClient(_app())
    for _ in range(3):
        assert client.post("/api/runtime/traces", content=b"x").status_code == 503

    assert len(calls) == 1
    assert "OTLP_RELAY_TARGET" in calls[0]