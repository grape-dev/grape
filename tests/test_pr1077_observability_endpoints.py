"""PR #1077 新增写端点的拒绝面原子断言。

覆盖 QA 原子层的 BLOCK 项：`POST /faro/collect` 与 `POST /api/runtime/traces`
此前没有任何原子引用。这里只断言最关键的拒绝面（未配置 → 503 disabled、
超限 → 413），不依赖外部 faro/LGTM 服务，可在 CI 离线执行。
"""
import os

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


def _faro_app(monkeypatch, upstream: str, max_body: int):
    from src.api import spa

    monkeypatch.setattr(spa, "_FARO_UPSTREAM", upstream, raising=True)
    monkeypatch.setattr(spa, "_FARO_MAX_BODY", max_body, raising=True)
    app = FastAPI()
    spa.install_spa_routes(app)
    return app


def test_faro_collect_disabled_when_upstream_not_configured(monkeypatch):
    """未配置 GRAPE_FARO_UPSTREAM → 503（不再回落到硬编码内网地址）。"""
    client = TestClient(_faro_app(monkeypatch, upstream="", max_body=1024))
    r = client.post("/faro/collect", json={"events": []})
    assert r.status_code == 503
    assert "disabled" in r.text


def test_faro_collect_rejects_oversized_payload(monkeypatch):
    """超过体上限 → 413（公开端点防滥用）。"""
    client = TestClient(_faro_app(monkeypatch, upstream="http://127.0.0.1:9/collect", max_body=16))
    r = client.post("/faro/collect", content=b"x" * 64,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_faro_collect_proxies_to_configured_upstream(monkeypatch):
    """上游可达时按上游状态码透传（用 127.0.0.1 的临时 HTTP server 模拟）。"""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):  # 静默
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        client = TestClient(_faro_app(monkeypatch, upstream=f"http://127.0.0.1:{port}/collect",
                                      max_body=4096))
        r = client.post("/faro/collect", json={"events": []})
        assert r.status_code == 204
    finally:
        srv.shutdown()


def _relay_app(monkeypatch, target: str, max_body: int | None = None):
    from src.api.routes import runtime as rt

    monkeypatch.setattr(rt, "OTLP_RELAY_TARGET", target, raising=True)
    if max_body is not None:
        monkeypatch.setenv("GRAPE_TRACE_RELAY_MAX_BODY", str(max_body))
    app = FastAPI()
    app.include_router(rt.router)
    # 绕过 runtime token 鉴权：本用例只验证拒绝面逻辑
    app.dependency_overrides[rt._require_credential] = lambda: {"id": 1, "runtime_id": 1}
    return app


def test_trace_relay_disabled_returns_503(monkeypatch):
    client = TestClient(_relay_app(monkeypatch, target=""))
    r = client.post("/api/runtime/traces", content=b"\x00\x01")
    assert r.status_code == 503
    assert "disabled" in r.text


def test_trace_relay_rejects_oversized_body(monkeypatch):
    client = TestClient(_relay_app(monkeypatch, target="http://127.0.0.1:9", max_body=8))
    r = client.post("/api/runtime/traces", content=b"x" * 64,
                    headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 413


def test_trace_relay_body_limit_default_is_two_mb(monkeypatch):
    """默认上限 2MB（OTLP 批量包余量足够）。"""
    from src.api.routes import runtime as rt

    monkeypatch.delenv("GRAPE_TRACE_RELAY_MAX_BODY", raising=False)
    client = TestClient(_relay_app(monkeypatch, target="http://127.0.0.1:9"))
    r = client.post("/api/runtime/traces", content=b"x" * (2 * 1024 * 1024 + 1),
                    headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 413

# ===== OTel 基座最小用例（架构守护 Critical-1 建议集）=====

def _isolate_otel_env(monkeypatch):
    """隔离 OTel 生效条件：清掉主控端点与 worker 中继两套环境变量。

    otel_enabled() 在 RUNTIME_TOKEN + RUNTIME_SERVER_URL 都存在时走 worker 中继分支
    返回 True（QA 第 4 轮指出：只删 OTEL_EXPORTER_OTLP_ENDPOINT 会让该用例在 worker
    主机或注入该环境的 CI 上必红）。
    """
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "RUNTIME_TOKEN", "RUNTIME_SERVER_URL",
              "OTLP_RELAY_TARGET"):
        monkeypatch.delenv(k, raising=False)


def test_otel_enabled_follows_endpoint_env(monkeypatch):
    """OTEL_EXPORTER_OTLP_ENDPOINT 为空 → 禁用；非空 → 启用。"""
    from src.observability import otel

    _isolate_otel_env(monkeypatch)
    assert otel.otel_enabled() is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert otel.otel_enabled() is True


def test_start_db_span_returns_none_without_parent(monkeypatch):
    """无有效父上下文时不建 span（避免孤儿 trace）。"""
    from src.observability import otel

    _isolate_otel_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert otel.start_db_span("SELECT 1") is None


def test_start_db_span_none_when_disabled(monkeypatch):
    from src.observability import otel

    _isolate_otel_env(monkeypatch)
    assert otel.start_db_span("SELECT 1") is None


def test_trace_context_roundtrip(monkeypatch):
    """入队 span 的 traceparent 能被 resume 还原为同一 trace_id（跨进程串联前提）。"""
    from src.observability import otel

    _isolate_otel_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    otel.init_otel("pr1077-test")
    tc = "traceparent|00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    ctx = otel.resume_trace_context(tc)
    assert ctx is not None
    from opentelemetry import context as otel_context

    token = otel_context.attach(ctx)
    try:
        snap = otel.snapshot_trace_context()
    finally:
        otel_context.detach(token)
    assert "4bf92f3577b34da6a3ce929d0e0e4736" in snap


def test_init_otel_is_process_level_idempotent(monkeypatch):
    """同一进程内不同 service_name 重复 init 不会重建 Provider（P2-4）。"""
    from src.observability import otel

    _isolate_otel_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    p1 = otel.init_otel("svc-one")
    p2 = otel.init_otel("svc-two")
    assert p1 is p2


# ===== 架构守护 BLOCKED 回归断言 =====

def test_worker_manifest_ships_otel_client():
    """BLOCKED-1：worker 包 MANIFEST 必须包含 observability 包入口与 otel 客户端，
    否则远程 worker 上所有 task/engine span 与 trace relay 静默失效。"""
    from src import worker_package

    manifest = list(worker_package.MANIFEST)
    assert "src/observability/__init__.py" in manifest
    assert "src/observability/otel.py" in manifest


def test_agent_tasks_trace_context_has_schema_migration():
    """BLOCKED-2：agent_tasks.trace_context 必须有建列迁移（新库不能靠手工 ALTER）。"""
    import inspect

    from src.api import db_setup

    src = inspect.getsource(db_setup)
    assert '_ensure_column(c, "agent_tasks", "trace_context"' in src


def test_observability_package_imports_without_prometheus(monkeypatch):
    """worker 包不带 prometheus_client 时，src.observability 仍必须可导入（otel 依赖它）。"""
    import importlib
    import sys

    from src import observability

    assert hasattr(observability, "_PROMETHEUS_AVAILABLE")
    # 桩实现路径：缺失 prometheus 时指标操作为 no-op，不抛异常
    if not observability._PROMETHEUS_AVAILABLE:
        observability.Counter("x", "y", ["l"]).labels(l="v").inc()
    assert importlib.import_module("src.observability").__name__ == "src.observability"
