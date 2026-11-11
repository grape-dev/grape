"""Issue #699 Phase 4 验收测试（TC-4.1 ~ TC-4.12）。

设计依据：docs/weekly-09/grape-unified-implementation-design.md §5（四进程设计）、
§8（sweep_loop 拆分）、§9.5（distributed_locks 分 key）、§10（nginx 反代）、
§11.3（systemd service）+ 设计文档「Phase 4 验证用例」。

覆盖范围（可单测部分）：
- TC-4.1 四进程模块/入口可导入，/health 各自返回 {"status":"ok","service":...}
- TC-4.2 Mgmt 进程用户面路由可达
- TC-4.3 Event 进程 webhook + events 操作 + Issue 入队端点可达
- TC-4.4 Worker 进程 runtime/tasks/chat 执行端点可达
- TC-4.5 Insight 进程 dashboard/obs/search/latency/health 路由可达
- TC-4.6 nginx 四进程配置存在且包含 4 upstream + 路径分流
- TC-4.7 定时任务按进程分配（server_tasks 四组 + 拆分 loop 存在性 + 进程 lifespan 引用）
- TC-4.8 systemd 4 个 service 文件存在且端口/ExecStart 正确
- TC-4.9 webhook→入队→认领→执行 链路端点齐备（Event 入队 + Worker 认领 + local_claimer）
- TC-4.10 Mgmt 进程注册 task_logs SSE 路由
- TC-4.11 table_maintenance 分 key（table_maintenance_tasks / table_maintenance_messages）
- TC-4.12 Insight 独立面板路由 + nginx /api/obs/ 分流到 insight upstream
"""
import os
import sys

os.environ.setdefault("GRAPE_AUTO_DETECT_URL", "false")

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import pytest
from fastapi.testclient import TestClient

from src.api import server_tasks
from src.api.grape_server import app as mgmt_app
from src.api.grape_event_server import app as event_app
from src.api.grape_worker_server import app as worker_app
from src.api.grape_insight_server import app as insight_app

SERVERS = {
    "mgmt": mgmt_app,
    "event": event_app,
    "worker": worker_app,
    "insight": insight_app,
}

# 设计 §TC-4.1 健康检查期望的 service 名
SERVICE_NAMES = {
    "mgmt": "server",
    "event": "event-server",
    "worker": "worker-server",
    "insight": "insight-server",
}

_LOOPBACK_HEADERS = {"X-Forwarded-For": "127.0.0.1"}


def _code(app, method, path, headers=None):
    with TestClient(app, headers=headers or {}) as c:
        return c.request(method, path).status_code


def _routes_of(app):
    """收集 app 全部有效路由路径（FastAPI 惰性 _IncludedRouter 用 effective_route_contexts 展开）。"""
    from fastapi.routing import APIRoute

    out = set()
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path:
            out.add(r.path)
            continue
        if type(r).__name__ == "_IncludedRouter":
            for ctx in r.effective_route_contexts():
                orig = getattr(ctx, "original_route", None)
                if orig is not None and getattr(orig, "path", None):
                    out.add(orig.path)
            continue
        p = getattr(r, "path", None)
        if p:
            out.add(p)
    return out


# ===========================================================================
# TC-4.1：四进程独立启动验证（模块可导入 + /health 正常）
# ===========================================================================

class TestTC41ProcessStartup:
    def test_server_modules_importable(self):
        for mod in ("src.api.grape_server", "src.api.grape_event_server",
                    "src.api.grape_worker_server", "src.api.grape_insight_server"):
            import importlib
            m = importlib.import_module(mod)
            assert m.app is not None

    def test_apps_entrypoints_importable(self):
        import importlib
        importlib.import_module("apps.mgmt.main")
        importlib.import_module("apps.runtime.webhook.main")
        importlib.import_module("apps.runtime.worker.main")
        importlib.import_module("apps.insight.main")

    @pytest.mark.parametrize("name", ["mgmt", "event", "worker", "insight"])
    def test_health_ok_per_process(self, name):
        """TC-4.1: 各进程 /health 返回 {"status":"ok","service":<预期 service 名>}。"""
        # 不用 context manager（避免触发 lifespan 启动定时任务）；XFF 头模拟内网访问
        c = TestClient(SERVERS[name], headers=_LOOPBACK_HEADERS)
        r = c.get("/health")
        assert r.status_code == 200, (name, r.status_code, r.text)
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == SERVICE_NAMES[name]

    @pytest.mark.parametrize("name", ["mgmt", "event", "worker", "insight"])
    def test_ping_reachable(self, name):
        r = TestClient(SERVERS[name]).get("/api/ping")
        assert r.status_code == 200


# ===========================================================================
# TC-4.2 ~ TC-4.5：各进程路由可达验证
# ===========================================================================

class TestTC42MgmtRoutes:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/repos/member"),
        ("GET", "/api/scenes"),
        ("GET", "/api/auth/profile"),
        ("GET", "/api/dashboard/ops/overview"),
        ("GET", "/api/search?q=test"),
        ("POST", "/api/latency/report"),
        ("GET", "/api/task-logs/x/stream"),
        ("GET", "/api/events/detail/1"),
        ("GET", "/api/issues/mine"),
        ("GET", "/api/runtime/install"),
    ])
    def test_mgmt_route_reachable(self, method, path):
        """TC-4.2: Mgmt 用户面路由均非 404（401/400/200 均为可达）。"""
        code = _code(mgmt_app, method, path)
        assert code != 404, f"{method} {path} -> {code}"
        assert code < 500, f"{method} {path} -> {code}"

    def test_mgmt_does_not_serve_worker_posts(self):
        """Mgmt 不服务 Worker 专属 POST 端点（404/405 均可，即非 2xx/4xx 可达）。"""
        code = _code(mgmt_app, "POST", "/api/runtime/register")
        assert code in (404, 405)


class TestTC43EventRoutes:
    @pytest.mark.parametrize("method,path", [
        ("POST", "/webhook/gitcode"),
        ("GET", "/api/a2a/status"),
        ("POST", "/api/events/1/terminate"),
        ("POST", "/api/events/1/redispatch"),
        ("POST", "/api/issues/test/repo"),
    ])
    def test_event_route_reachable(self, method, path):
        """TC-4.3: Event 进程 webhook + 操作端点均非 404。"""
        code = _code(event_app, method, path)
        assert code != 404, f"{method} {path} -> {code}"
        assert code < 500, f"{method} {path} -> {code}"

    def test_event_does_not_serve_mgmt_gets(self):
        """Event 不服务 Mgmt 用户面 GET（events 查看在 Mgmt）。"""
        assert _code(event_app, "GET", "/api/events/detail/1") == 404
        assert _code(event_app, "GET", "/api/scenes") == 404


class TestTC44WorkerRoutes:
    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/runtime/register"),
        ("POST", "/api/runtime/heartbeat"),
        ("POST", "/api/runtime/runtimes/1/tasks/claim"),
        ("GET", "/api/runtime/tasks/1/status"),
        ("POST", "/api/tasks/complete"),
        ("POST", "/api/tasks/notify"),
        ("GET", "/api/tasks/current"),
        ("POST", "/api/chat/send/stream"),
    ])
    def test_worker_route_reachable(self, method, path):
        """TC-4.4: Worker 进程 POST/GET 端点均非 404。"""
        code = _code(worker_app, method, path)
        assert code != 404, f"{method} {path} -> {code}"
        assert code < 500, f"{method} {path} -> {code}"

    def test_worker_does_not_serve_mgmt_routes(self):
        assert _code(worker_app, "GET", "/api/repos/member") == 404


class TestTC45InsightRoutes:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/dashboard/ops/overview"),
        ("GET", "/api/obs/overview"),
        ("GET", "/api/obs/alerts"),
        ("GET", "/api/search?q=test"),
        ("POST", "/api/latency/report"),
        ("GET", "/api/health"),
    ])
    def test_insight_route_reachable(self, method, path):
        """TC-4.5: Insight 进程 dashboard/obs/search/latency/health 均非 404。"""
        code = _code(insight_app, method, path)
        assert code != 404, f"{method} {path} -> {code}"
        assert code < 500, f"{method} {path} -> {code}"


# ===========================================================================
# TC-4.6：nginx 四进程反代配置验证（静态产物）
# ===========================================================================

class TestTC46NginxConfig:
    CONF = os.path.join(ROOT, "deploy", "nginx-grape-four-process.conf")

    def _conf(self):
        with open(self.CONF, encoding="utf-8") as f:
            return f.read()

    def test_conf_exists(self):
        assert os.path.isfile(self.CONF)

    def test_four_upstreams(self):
        conf = self._conf()
        for up in ("grape_server", "grape_event_server", "grape_worker_server", "grape_insight_server"):
            assert f"upstream {up} {{" in conf
        assert "127.0.0.1:38080" in conf
        assert "127.0.0.1:38081" in conf
        assert "127.0.0.1:38082" in conf
        assert "127.0.0.1:38083" in conf

    def test_path_split_locations(self):
        conf = self._conf()
        for loc in ("location /api/runtime/", "location ~ ^/api/tasks/",
                    "location /webhook/", "location /api/a2a",
                    "location ~ ^/api/events/", "location /api/obs/",
                    "location /api/dashboard/", "location /api/search",
                    "location /api/latency", "location /api/task-logs/",
                    "location /api/chat/",
                    "location /"):
            assert loc in conf, f"missing location {loc}"

    def test_chat_routes_to_worker(self):
        """TC-4.6: /api/chat/ 分流到 Worker（proxy_pass grape_worker_server）。"""
        conf = self._conf()
        assert "location /api/chat/" in conf
        # 找到 /api/chat/ location 块，验证 proxy_pass 指向 worker
        chat_block = conf.split("location /api/chat/")[1].split("location")[0]
        assert "grape_worker_server" in chat_block, "/api/chat/ should proxy to grape_worker_server"
        assert "proxy_buffering off" in chat_block, "/api/chat/ should disable proxy_buffering for SSE"

    def test_chat_reads_route_to_mgmt(self):
        """chat 读端点（GET conversations/messages/...）→ Mgmt，Worker 只挂 POST 会 405/404。"""
        import re
        conf = self._conf()
        m = re.search(r"location ~ (\^/api/chat/\([^)]*\)) \{", conf)
        assert m, "chat read-endpoint regex location missing"
        read_block = conf[m.end():].split("location ")[0]
        assert "grape_server" in read_block
        assert "grape_worker_server" not in read_block
        pattern = re.compile(m.group(1))
        for path in ("/api/chat/conversations", "/api/chat/messages/9",
                     "/api/chat/config", "/api/chat/task/t9/stream",
                     "/api/chat/analyze-image", "/api/chat/image/9/a.png"):
            assert pattern.match(path), path
        for path in ("/api/chat/send", "/api/chat/send/stream", "/api/chat/submit"):
            assert not pattern.match(path), path


# ===========================================================================
# TC-4.7：定时任务按进程分配验证
# ===========================================================================

class TestTC47TaskAllocation:
    def test_sweep_split_functions_exist(self):
        from src.domains.runtime.engine import cleanup
        assert callable(cleanup.sweep_events_loop)
        assert callable(cleanup.sweep_tasks_loop)
        assert callable(cleanup.sweep_loop)  # 单进程兼容保留

    def test_server_tasks_four_groups(self):
        for fn in ("start_mgmt_tasks", "start_event_tasks", "start_worker_tasks", "start_insight_tasks",
                   "stop_mgmt_tasks", "stop_event_tasks", "stop_worker_tasks", "stop_insight_tasks"):
            assert callable(getattr(server_tasks, fn)), fn

    def test_event_server_starts_event_group_only(self):
        import inspect
        import src.api.grape_event_server as mod
        src_code = inspect.getsource(mod)
        assert "start_event_tasks" in src_code
        assert "start_mgmt_tasks" not in src_code
        assert "start_worker_tasks" not in src_code
        assert "start_insight_tasks" not in src_code

    def test_worker_server_starts_worker_group_only(self):
        import inspect
        import src.api.grape_worker_server as mod
        src_code = inspect.getsource(mod)
        assert "start_worker_tasks" in src_code
        assert "start_mgmt_tasks" not in src_code
        assert "start_event_tasks" not in src_code

    def test_insight_server_starts_insight_group_only(self):
        import inspect
        import src.api.grape_insight_server as mod
        src_code = inspect.getsource(mod)
        assert "start_insight_tasks" in src_code
        assert "start_mgmt_tasks" not in src_code
        assert "start_worker_tasks" not in src_code

    def test_mgmt_server_starts_mgmt_group_only(self):
        import inspect
        import src.api.grape_server as mod
        src_code = inspect.getsource(mod)
        assert "start_mgmt_tasks" in src_code
        assert "start_event_tasks" not in src_code
        assert "start_worker_tasks" not in src_code
        assert "start_insight_tasks" not in src_code

    def test_worker_group_includes_claim_and_schedule_sources(self):
        """TC-4.7: Worker 组源码含 local_claimer / scheduler / health_refresh / sweep_tasks。"""
        import inspect
        src_code = inspect.getsource(server_tasks.start_worker_tasks)
        for sym in ("start_local_claimer", "start_scheduler", "start_health_refresh_scheduler",
                    "sweep_tasks_loop", "table_maintenance_tasks_loop", "probe_local_engines",
                    "reap_stale_runtimes"):
            assert sym in src_code, sym

    def test_insight_group_includes_obs_sources(self):
        """TC-4.7: Insight 组源码含 obs_rollup / obs_sample / obs_alert / server_metrics。"""
        import inspect
        src_code = inspect.getsource(server_tasks.start_insight_tasks)
        for sym in ("evaluate_rules", "catch_up", "snapshot_runtimes",
                    "install_master_logging", "stamp_missing", "table_maintenance_messages_loop"):
            assert sym in src_code, sym


# ===========================================================================
# TC-4.8：systemd service 文件验证（部署产物静态校验）
# ===========================================================================

class TestTC48SystemdUnits:
    SYSTEMD_DIR = os.path.join(ROOT, "deploy", "systemd")

    @pytest.mark.parametrize("name,port,role,workers", [
        ("grape-server.service", "38080", "server", "2"),
        ("grape-event-server.service", "38081", "event", "1"),
        ("grape-worker-server.service", "38082", "worker", "2"),
        ("grape-insight-server.service", "38083", "insight", "1"),
    ])
    def test_unit_present_and_correct(self, name, port, role, workers):
        path = os.path.join(self.SYSTEMD_DIR, name)
        assert os.path.isfile(path), f"{name} missing"
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert f"GRAPE_PORT={port}" in content
        assert f"GRAPE_PROCESS_ROLE={role}" in content
        assert f"--port {port} --workers {workers}" in content
        assert "src.api.grape_" in content


# ===========================================================================
# TC-4.9：webhook → 入队 → 认领 → 执行 链路端点齐备
# ===========================================================================

class TestTC49ChainEndpoints:
    def test_event_enqueue_endpoint(self):
        """Event 进程持有 Issue 入队端点（POST /api/issues/{owner}/{repo}）。"""
        paths = _routes_of(event_app)
        assert "/api/issues/{owner}/{repo}" in paths, "issues create enqueue missing on Event"

    def test_worker_claim_endpoints(self):
        """Worker 进程持有认领/完成端点。"""
        paths = _routes_of(worker_app)
        for p in ("/api/runtime/runtimes/{runtime_id}/tasks/claim",
                  "/api/runtime/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}",
                  "/api/tasks/complete"):
            assert p in paths, f"{p} missing on Worker"

    def test_local_claimer_claim_chain_importable(self):
        """TC-4.9: local_claimer 确定性认领接口（Phase 3 闭环）在 Worker 路径可导入。"""
        import src.domains.runtime.engine.local_claimer as lc
        assert callable(lc.start_local_claimer)


# ===========================================================================
# TC-4.10：SSE 任务日志流跨进程（Mgmt 注册 task_logs）
# ===========================================================================

class TestTC410TaskLogSSE:
    def test_mgmt_registers_task_logs_stream(self):
        paths = _routes_of(mgmt_app)
        assert "/api/task-logs/{task_id}/stream" in paths, "task_logs SSE missing on Mgmt"
        assert "/api/task-logs/{task_id}/messages" in paths


# ===========================================================================
# TC-4.11：distributed_locks 四进程按 key 隔离
# ===========================================================================

class TestTC411DistributedLocks:
    def test_table_maintenance_split_lock_keys(self):
        import inspect
        from src.domains.runtime.engine import table_maintenance as tm
        tasks_src = inspect.getsource(tm.table_maintenance_tasks_loop)
        msgs_src = inspect.getsource(tm.table_maintenance_messages_loop)
        assert "table_maintenance_tasks" in tasks_src
        assert "table_maintenance_messages" in msgs_src
        # 两 key 互异，避免 Worker/Insight 互相阻塞（设计 §9.5）
        assert "table_maintenance_tasks" not in msgs_src


# ===========================================================================
# TC-4.12：Insight 独立面板验证（路由 + nginx 分流）
# ===========================================================================

class TestTC412InsightIndependent:
    def test_insight_obs_routes_registered(self):
        paths = _routes_of(insight_app)
        assert "/api/obs/overview" in paths
        assert "/api/obs/alerts" in paths
        assert "/api/dashboard/ops/overview" in paths

    def test_nginx_routes_obs_to_insight(self):
        with open(os.path.join(ROOT, "deploy", "nginx-grape-four-process.conf"), encoding="utf-8") as f:
            conf = f.read()
        obs_block = conf.split("location /api/obs/", 1)[1].split("}", 1)[0]
        assert "grape_insight_server" in obs_block