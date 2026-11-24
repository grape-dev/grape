"""Issue #3699：worker trace 中继 503 刷屏修复与降噪（原子契约测试）。

覆盖三层契约：
1. 主控中继禁用态带确定性标记头（body 契约不变） + 进程内一次性 WARNING；
2. 访问日志分级：仅「/api/runtime/traces + 503 + 禁用标记」降为 debug，
   上游透传的 503（无标记）仍按 ERROR；
3. worker 侧 exporter：禁用 → 一次性 WARNING + 指数退避静默窗口；
   其他 5xx / 网络错误保持可重试并保留 ERROR 收尾；2xx 复位。

不依赖外网 / LGTM：中继侧用 TestClient + 依赖覆盖，worker 侧用注入的假 post。
"""
import logging

import pytest
from fastapi import FastAPI
from starlette.responses import Response
from starlette.testclient import TestClient

_LOG = "devops-bot"


# ===== 1. 主控中继禁用态标记 =====

def _relay_app(monkeypatch, target: str = ""):
    from src.domains.runtime.routes import runtime as rt

    # 目标值按请求现读环境变量（模块常量已移除，见 tests/runtime/test_trace_relay_env_and_metrics.py）
    monkeypatch.setenv("OTLP_RELAY_TARGET", target)
    monkeypatch.setattr(rt, "_relay_warn_ts", 0.0, raising=True)
    app = FastAPI()
    app.include_router(rt.router)
    app.dependency_overrides[rt._require_credential] = lambda: {"id": 1, "runtime_id": 1}
    return app


def test_disabled_relay_response_carries_marker(monkeypatch, caplog):
    """禁用中继：503 + X-Grape-Trace-Relay: disabled，body 契约不变，且只告警一次。"""
    client = TestClient(_relay_app(monkeypatch))

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        first = client.post("/api/runtime/traces", content=b"\x00\x01")
        second = client.post("/api/runtime/traces", content=b"\x00\x01")

    assert first.status_code == 503
    assert first.headers.get("x-grape-trace-relay") == "disabled"
    assert "trace relay disabled" in first.text
    assert second.status_code == 503
    assert second.headers.get("x-grape-trace-relay") == "disabled"

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "OTLP_RELAY_TARGET" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_relay_success_response_has_no_disabled_marker(monkeypatch):
    """正常中继（上游 200）不带禁用标记，下游分级不受影响。"""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        client = TestClient(_relay_app(monkeypatch, target=f"http://127.0.0.1:{port}"))
        r = client.post("/api/runtime/traces", content=b"\x00\x01")
        assert r.status_code == 200
        assert r.headers.get("x-grape-trace-relay") is None
    finally:
        srv.shutdown()


# ===== 2. 访问日志分级 =====

def _log_app(install):
    app = FastAPI()

    @app.post("/api/runtime/traces")
    async def relay_disabled():
        return Response(status_code=503, content='{"detail":"trace relay disabled"}',
                        media_type="application/json",
                        headers={"X-Grape-Trace-Relay": "disabled"})

    @app.post("/api/runtime/upstream")
    async def relay_upstream_503():
        return Response(status_code=503, content='{"detail":"lgmt down"}',
                        media_type="application/json")

    install(app)
    return app


def _records_for(caplog, path):
    return [r for r in caplog.records if path in r.getMessage()]


def _install_server_common(app):
    from src.api import server_common
    server_common._attach_request_log(app)


def _install_middleware(app):
    from src.api import middleware
    middleware.install_request_middleware(app)


@pytest.mark.parametrize("install", [_install_server_common, _install_middleware])
def test_relay_disabled_503_logs_debug(install, caplog):
    """带禁用标记的 503 不再刷 ERROR（配置缺失是 feature-off，不是事故）。"""
    client = TestClient(_log_app(install))
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.post("/api/runtime/traces")
    assert r.status_code == 503
    records = _records_for(caplog, "/api/runtime/traces")
    assert records, "请求日志缺失（分级未生效）"
    assert all(rec.levelno < logging.ERROR for rec in records), \
        [(rec.levelname, rec.getMessage()) for rec in records]


@pytest.mark.parametrize("install", [_install_server_common, _install_middleware])
def test_relay_upstream_503_still_error(install, caplog):
    """无禁用标记的 503（上游 LGTM 故障透传）仍按 ERROR，不被误抑制。"""
    client = TestClient(_log_app(install))
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        r = client.post("/api/runtime/upstream")
    assert r.status_code == 503
    errors = [rec for rec in _records_for(caplog, "/api/runtime/upstream")
              if rec.levelno == logging.ERROR]
    assert errors, [(rec.levelname, rec.getMessage()) for rec in caplog.records]


# ===== 3. worker 侧禁用识别 / 退避策略 =====

def test_disabled_kind_classification():
    """只有 503 + trace relay disabled 判为确定性禁用；其他 5xx/网络错误仍可重试。"""
    from src.kernel import otel

    assert otel.relay_failure_kind(503, '{"detail":"trace relay disabled"}') == "disabled"
    assert otel.relay_failure_kind(503, "Trace Relay Disabled") == "disabled"
    assert otel.relay_failure_kind(503, '{"detail":"lgmt unreachable"}') == "retryable"
    assert otel.relay_failure_kind(502, "") == "retryable"
    assert otel.relay_failure_kind(500, "") == "retryable"
    assert otel.relay_failure_kind(413, '{"detail":"payload too large"}') == "fatal"


def test_backoff_grows_and_caps():
    """禁用退避：30s 起、逐次翻倍、上限 900s；窗口内判定为抑制。"""
    from src.kernel import otel

    policy = otel._RelayBackoffPolicy()
    assert policy.in_window(now=1000.0) is False

    assert policy.note_disabled(now=1000.0) is True     # 首次 → 允许告警
    assert policy.delay == 30.0
    assert policy.in_window(now=1000.0 + 29.0) is True

    assert policy.note_disabled(now=1000.0 + 30.0) is False   # 已告警过 → 静默
    assert policy.delay == 60.0

    for step in range(20):
        policy.note_disabled(now=2000.0 + step * 900.0)
    assert policy.delay == 900.0


def test_backoff_resets_on_success():
    """中继恢复（2xx）后退避状态复位，可再次告警。"""
    from src.kernel import otel

    policy = otel._RelayBackoffPolicy()
    assert policy.note_disabled(now=0.0) is True
    policy.note_success()
    assert policy.delay == 30.0
    assert policy.in_window(now=10_000.0) is False
    assert policy.note_disabled(now=10_000.0) is True


# ===== 4. worker 侧 exporter 分支行为 =====

class _FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _one_span():
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("issue-3699-test")
    span = tracer.start_span("unit")
    span.end()
    return [span]


def _exporter(posts, policy=None, sleep=None):
    from src.kernel import otel

    calls = []

    def _post(url, payload, headers, timeout):
        calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        item = posts[min(len(calls) - 1, len(posts) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    exp = otel._RelaySpanExporter(
        "http://master.test/api/runtime/traces",
        headers={"Authorization": "Bearer unit"},
        policy=policy,
        post=_post,
        sleep=sleep or (lambda s: None),
    )
    return exp, calls


def test_disabled_export_warns_once_and_suppresses(caplog):
    """禁用态：一次 WARNING、无 ERROR；静默窗口内不再发起上报（不刷主控日志）。"""
    from src.kernel import otel

    policy = otel._RelayBackoffPolicy()
    exp, calls = _exporter([_FakeResp(503, '{"detail":"trace relay disabled"}')],
                           policy=policy)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        first = exp.export(_one_span())
        second = exp.export(_one_span())

    from opentelemetry.sdk.trace.export import SpanExportResult

    assert first == SpanExportResult.FAILURE
    assert second == SpanExportResult.FAILURE
    assert len(calls) == 1, "静默窗口内仍发起了上报请求"
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert [lvl for lvl, _ in msgs].count("WARNING") == 1, msgs
    assert not [m for lvl, m in msgs if lvl == "ERROR"], msgs
    assert "OTLP_RELAY_TARGET" in msgs[0][1]


def test_retryable_export_retries_then_errors(caplog):
    """502：保持可重试（多次重试），最终 ERROR 不被抑制。"""
    from src.kernel import otel
    from opentelemetry.sdk.trace.export import SpanExportResult

    exp, calls = _exporter([_FakeResp(502, "bad gateway")] * 3,
                           policy=otel._RelayBackoffPolicy())

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        result = exp.export(_one_span())

    assert result == SpanExportResult.FAILURE
    assert len(calls) == 3, "可重试错误未执行重试"
    assert [r for r in caplog.records if r.levelno == logging.ERROR], \
        [(r.levelname, r.getMessage()) for r in caplog.records]
    assert not [r for r in caplog.records if "trace relay disabled" in r.getMessage()]


def test_network_error_keeps_retrying(caplog):
    """网络错误同样按可重试处理，不进入禁用静默。"""
    from src.kernel import otel
    from opentelemetry.sdk.trace.export import SpanExportResult

    policy = otel._RelayBackoffPolicy()
    exp, calls = _exporter([ConnectionError("refused")] * 3, policy=policy)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        result = exp.export(_one_span())

    assert result == SpanExportResult.FAILURE
    assert len(calls) == 3
    assert policy.in_window() is False
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


def test_success_export_resets_policy(caplog):
    """2xx：SUCCESS，且此前累积的禁用退避状态复位。"""
    from src.kernel import otel
    from opentelemetry.sdk.trace.export import SpanExportResult

    policy = otel._RelayBackoffPolicy()
    policy.note_disabled(now=0.0)
    exp, calls = _exporter([_FakeResp(200, "")], policy=policy)

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        result = exp.export(_one_span())

    assert result == SpanExportResult.SUCCESS
    assert len(calls) == 1
    assert policy.delay == 30.0
    assert policy.in_window(now=10_000.0) is False


def test_exporter_posts_otlp_protobuf_with_bearer():
    """上报走 OTLP/HTTP protobuf + 既有 Bearer 头（主控中继契约不变）。"""
    from src.kernel import otel

    exp, calls = _exporter([_FakeResp(200, "")])
    exp.export(_one_span())

    assert calls[0]["url"] == "http://master.test/api/runtime/traces"
    assert calls[0]["headers"]["Authorization"] == "Bearer unit"
    assert calls[0]["headers"]["Content-Type"] == "application/x-protobuf"