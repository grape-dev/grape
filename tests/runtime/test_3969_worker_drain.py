"""Issue #3969：worker 停机先摘流排空、启动 readiness 门禁。

漂移形态：uvicorn 收到 SIGTERM 后先关闭 8083 监听再排空（57s），systemd
`Restart=always` 冷启动 36s，全程无摘流/无 readiness 门禁 → 91s 全量 502。

seam：
- `src/api/drain_state.py` 文件型排空标记（多 uvicorn worker 共享，回环/进程一致）；
- `server_common.attach_health(..., drain_file=...)` 排空期间 `/readyz` 503；
- worker 进程 `readiness=True` + `POST /internal/drain`（仅回环）；
- systemd 单元含 ExecStop 摘流脚本、ExecStartPost readiness 轮询与放大超时；
- `run.py` SIGTERM 在 terminate 前先摘流并等待排空窗口。
"""
import inspect
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import drain_state
from src.api.server_common import attach_health

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SYSTEMD = os.path.join(_REPO_ROOT, "deploy/systemd/grape-worker-server.service")
_LOOPBACK = {"X-Forwarded-For": "127.0.0.1"}


@pytest.fixture()
def drain_file(tmp_path, monkeypatch):
    path = str(tmp_path / "worker-draining")
    monkeypatch.setenv("GRAPE_DRAIN_FILE", path)
    drain_state.clear_drain()
    yield path
    drain_state.clear_drain()


class TestDrainState:
    def test_begin_and_clear(self, drain_file):
        assert drain_state.is_draining() is False
        drain_state.begin_drain()
        assert drain_state.is_draining() is True
        drain_state.clear_drain()
        assert drain_state.is_draining() is False

    def test_not_configured_is_never_draining(self, monkeypatch):
        monkeypatch.delenv("GRAPE_DRAIN_FILE", raising=False)
        assert drain_state.is_draining() is False
        # 未配置时 begin_drain 为无操作，不应在默认路径留下标记
        drain_state.begin_drain()
        assert drain_state.is_draining() is False

    def test_drain_seconds_env_and_floor(self, monkeypatch):
        monkeypatch.setenv("GRAPE_DRAIN_SECONDS", "7")
        assert drain_state.drain_seconds() == 7
        monkeypatch.setenv("GRAPE_DRAIN_SECONDS", "-3")
        assert drain_state.drain_seconds() == 0
        monkeypatch.setenv("GRAPE_DRAIN_SECONDS", "bad")
        assert drain_state.drain_seconds() == 5


class TestReadinessGate:
    def _app(self, drain_file):
        app = FastAPI()
        attach_health(app, service_name="worker-server", readiness=True,
                      drain_file=drain_file)
        return app

    def test_ready_when_not_draining(self, drain_file):
        c = TestClient(self._app(drain_file), headers=_LOOPBACK)
        r = c.get("/readyz")
        # DB 未必可用，但 draining 检查必须显式暴露为 false
        assert r.json()["checks"].get("draining") in (None, "false")

    def test_not_ready_while_draining(self, drain_file):
        drain_state.begin_drain()
        c = TestClient(self._app(drain_file), headers=_LOOPBACK)
        r = c.get("/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["draining"] == "draining"


class TestWorkerServerWiring:
    def test_readiness_enabled_and_drain_route(self):
        from src.api import grape_worker_server
        src = inspect.getsource(grape_worker_server)
        assert "readiness=True" in src
        assert "/internal/drain" in src
        paths = {getattr(r, "path", "") for r in grape_worker_server.app.routes}
        assert "/internal/drain" in paths


class TestSystemdUnit:
    def _unit(self):
        with open(_SYSTEMD, encoding="utf-8") as f:
            return f.read()

    def test_exec_stop_drains_before_sigterm(self):
        unit = self._unit()
        assert "ExecStop=" in unit
        assert "grape-worker-drain.sh" in unit
        assert "GRAPE_DRAIN_SECONDS=5" in unit

    def test_start_readiness_gate_non_blocking(self):
        unit = self._unit()
        assert "ExecStartPost=-" in unit
        assert "grape-worker-ready.sh" in unit
        assert "TimeoutStartSec=" in unit and "TimeoutStopSec=" in unit

    def test_drain_scripts_shipped(self):
        for name in ("grape-worker-drain.sh", "grape-worker-ready.sh"):
            assert os.path.isfile(os.path.join(os.path.dirname(_SYSTEMD), name)), name


class TestRunShutdownOrder:
    def test_main_drain_uses_separate_marker(self, tmp_path, monkeypatch):
        from src.api import run
        import time as _time

        main_marker = str(tmp_path / "main-webhook-draining")
        worker_marker = str(tmp_path / "worker-draining")
        monkeypatch.setenv("GRAPE_MAIN_DRAIN_FILE", main_marker)
        monkeypatch.setenv("GRAPE_DRAIN_FILE", worker_marker)

        slept = []
        orig_sleep = _time.sleep
        try:
            _time.sleep = lambda s: slept.append(s)
            run._drain_before_stop()
        finally:
            _time.sleep = orig_sleep
        # main+webhook 只写自己的标记，不得污染 worker readiness 标记（复审 R6）
        assert drain_state.is_main_draining() is True
        assert os.path.exists(main_marker)
        assert not os.path.exists(worker_marker)
        assert drain_state.is_draining() is False
        assert slept and slept[0] >= 5

    def test_no_drain_without_config(self, monkeypatch):
        from src.api import run
        monkeypatch.delenv("GRAPE_MAIN_DRAIN_FILE", raising=False)
        assert run._drain_before_stop() == 0

    def test_shutdown_calls_drain_before_terminate(self):
        from src.api import run
        src = inspect.getsource(run._shutdown)
        assert src.index("_drain_before_stop()") < src.index("p1.terminate()")


class TestDrainRoleIsolation:
    def test_main_drain_does_not_affect_worker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAPE_MAIN_DRAIN_FILE", str(tmp_path / "main-draining"))
        monkeypatch.setenv("GRAPE_DRAIN_FILE", str(tmp_path / "worker-draining"))
        drain_state.begin_main_drain()
        assert drain_state.is_main_draining() is True
        assert drain_state.is_draining() is False
        drain_state.clear_main_drain()
        assert drain_state.is_main_draining() is False

    def test_worker_drain_does_not_affect_main(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAPE_MAIN_DRAIN_FILE", str(tmp_path / "main-draining"))
        monkeypatch.setenv("GRAPE_DRAIN_FILE", str(tmp_path / "worker-draining"))
        drain_state.begin_drain()
        assert drain_state.is_draining() is True
        assert drain_state.is_main_draining() is False
        drain_state.clear_drain()
        assert drain_state.is_draining() is False

    def test_clear_respects_configured(self, monkeypatch, tmp_path):
        # 复审 R6：clear_drain 与 begin_drain 判定对齐，未配置时为无操作。
        monkeypatch.delenv("GRAPE_DRAIN_FILE", raising=False)
        stale = tmp_path / "stale-worker-draining"
        stale.write_text("x")
        drain_state.clear_drain(str(stale))
        assert stale.exists()


class TestInternalDrainAuth:
    """复审 R7：/internal/drain 只认 TCP 对端，不信任 X-Forwarded-For。"""

    class _Req:
        def __init__(self, host, xff=""):
            from types import SimpleNamespace
            self.client = SimpleNamespace(host=host)
            self.headers = {"x-forwarded-for": xff} if xff else {}

    def _endpoint(self):
        import asyncio
        from src.api import grape_worker_server
        route = next(r for r in grape_worker_server.app.routes
                     if getattr(r, "path", "") == "/internal/drain")
        return route.endpoint, asyncio.run

    def test_spoofed_xff_from_non_loopback_is_forbidden(self, drain_file):
        endpoint, run = self._endpoint()
        resp = run(endpoint(self._Req("10.0.0.7", "127.0.0.1")))
        assert getattr(resp, "status_code", 200) == 403
        assert drain_state.is_draining() is False

    def test_direct_loopback_is_allowed(self, drain_file):
        endpoint, run = self._endpoint()
        resp = run(endpoint(self._Req("127.0.0.1")))
        assert isinstance(resp, dict)
        assert resp["status"] == "draining"
        assert drain_state.is_draining() is True

def test_main_attach_health_wires_main_drain_file():
    """复审 R6：main 的 /readyz 必须挂上角色隔离的 main 标记，否则摘流对本进程无效。"""
    main_py = os.path.join(_REPO_ROOT, "src/api/main.py")
    with open(main_py, encoding="utf-8") as f:
        src = f.read()
    assert "main_drain_file_path()" in src
    assert "is_main_configured()" in src
