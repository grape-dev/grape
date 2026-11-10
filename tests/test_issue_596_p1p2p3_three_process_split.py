"""P1-P3 tests for Issue #596: three-process split.

Covers (design doc docs/weekly-09/grape-three-process-split-design.md):
- P1: three entry modules exist and expose FastAPI apps with the right
  route sets (A: user-facing; B: webhook+events ops; C: runtime+tasks)
- P1: runtime_view.py re-registers user-facing GET/PUT endpoints from runtime.py
- P1: events.py exposes ops_router with B-process operation endpoints
- P1: nginx config routes the three upstreams; systemd units for 3 services
- P2: sweep_once split — sweep_events_loop (B) + sweep_tasks_loop (C)
- P3: table_maintenance split lock keys (tasks → C, messages → A)
"""
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]  # worktree root = repo root (tests/ sits at top level)


# ---------------------------------------------------------------- P1: entries

class TestProcessEntries:
    def test_grape_server_module_importable(self):
        from src.api import grape_server
        assert grape_server.app is not None

    def test_grape_event_server_module_importable(self):
        from src.api import grape_event_server
        assert grape_event_server.app is not None

    def test_grape_worker_server_module_importable(self):
        from src.api import grape_worker_server
        assert grape_worker_server.app is not None

    def test_a_process_routes_user_facing(self):
        """A 进程包含用户面路由前缀，且不含 webhook（B 职责）。"""
        from src.api import grape_server
        prefixes = _app_paths(grape_server.app)
        for expect in ("/api/auth", "/api/repos", "/api/dashboard", "/api/chat",
                       "/api/issues", "/api/admin", "/api/git-repo"):
            assert any(p.startswith(expect) for p in prefixes), f"A missing {expect}"
        assert not any(p.startswith("/webhook") for p in prefixes), \
            "A process must not serve /webhook (B responsibility)"

    def test_b_process_routes_webhook_and_events_ops(self):
        """B 进程包含 webhook + A2A + events 操作端点，且不含用户面查看端点。"""
        from src.api import grape_event_server
        prefixes = _app_paths(grape_event_server.app)
        assert any(p.startswith("/webhook/") for p in prefixes), "B missing /webhook/"
        assert any(p.startswith("/api/a2a") for p in prefixes), "B missing /api/a2a"
        # events 操作端点在 B
        assert any(p.startswith("/api/events/") and p.endswith("/terminate")
                   for p in prefixes), "B missing events terminate"
        assert any(p.startswith("/api/events/") and p.endswith("/redispatch")
                   for p in prefixes), "B missing events redispatch"
        # GET 详情查看端点不在 B
        assert not any(p == "/api/events/detail/{seq}" for p in prefixes), \
            "B must not serve GET /events/detail (A responsibility)"
        # 用户面路由不在 B
        assert not any(p.startswith("/api/dashboard") for p in prefixes), \
            "B must not serve user-facing dashboard"

    def test_c_process_routes_runtime_and_tasks(self):
        """C 进程包含 runtime Worker 协议 + tasks 自报端点。"""
        from src.api import grape_worker_server
        prefixes = _app_paths(grape_worker_server.app)
        for expect in ("/api/runtime/register", "/api/runtime/heartbeat",
                       "/api/tasks/notify", "/api/tasks/complete", "/api/tasks/current"):
            assert any(p.startswith(expect) for p in prefixes), f"C missing {expect}"

    def test_c_process_has_local_claimer_in_lifespan_source(self):
        from src.api import grape_worker_server
        src = Path(grape_worker_server.__file__).read_text(encoding="utf-8")
        assert "start_local_claimer" in src, "C lifespan must start local_claimer"
        assert "sweep_tasks_loop" in src, "C lifespan must start sweep_tasks_loop (P2)"

    def test_b_process_has_sweep_events_loop_in_lifespan_source(self):
        from src.api import grape_event_server
        src = Path(grape_event_server.__file__).read_text(encoding="utf-8")
        assert "sweep_events_loop" in src, "B lifespan must start sweep_events_loop (P2)"
        assert "start_bot_token_health" in src, "B lifespan must start bot_token_health"

    def test_a_process_has_table_maintenance_messages_only(self):
        from src.api import grape_server
        src = Path(grape_server.__file__).read_text(encoding="utf-8")
        assert "table_maintenance_messages" in src
        assert "table_maintenance_tasks" not in src, \
            "A must not run agent_tasks table maintenance (C responsibility)"

    def test_c_process_has_table_maintenance_tasks_only(self):
        from src.api import grape_worker_server
        src = Path(grape_worker_server.__file__).read_text(encoding="utf-8")
        assert "table_maintenance_tasks" in src
        assert "table_maintenance_messages" not in src, \
            "C must not run task_messages table maintenance (A responsibility)"


def _iter_app_routes(app):
    """产出 app 上全部注册的 APIRoute 对象（兼容 fastapi>=0.141 惰性 _IncludedRouter）。

    fastapi>=0.141：include_router 产生惰性 _IncludedRouter（无 .path），旧写法
    `[r.path for r in app.routes if hasattr(r, "path")]` 会漏掉全部子路由。
    展开其 effective_candidates().original_route 取实际注册的 APIRoute
    （路由确已注册，可用 app.openapi() 交叉验证）。
    """
    for r in app.routes:
        if getattr(r, "path", None):
            yield r
            continue
        try:
            for cand in r.effective_candidates():
                orr = getattr(cand, "original_route", None) or cand
                if getattr(orr, "path", None):
                    yield orr
        except Exception:
            pass


def _app_paths(app) -> list[str]:
    """收集 app 上全部注册路由的 path。"""
    return [r.path for r in _iter_app_routes(app)]


# ---------------------------------------------------- P1: runtime_view router

class TestRuntimeViewRouter:
    def test_runtime_view_router_importable(self):
        from src.api.routes import runtime_view
        assert runtime_view.view_router is not None
        assert runtime_view.router is runtime_view.view_router  # backward-compat alias

    def test_runtime_view_has_user_facing_get_endpoints(self):
        from src.api.routes import runtime_view
        paths = {(r.path, tuple(sorted(r.methods))) for r in runtime_view.view_router.routes}
        expected = {
            ("/api/runtime/admin/machine-health", ("GET",)),
            ("/api/runtime/admin/machine-health/{runtime_id}", ("GET",)),
            ("/api/runtime/tasks/{task_id}/status", ("GET",)),
            ("/api/runtime/runtimes/{runtime_id}/tasks", ("GET",)),
            ("/api/runtime/install", ("GET",)),
            ("/api/runtime/grape-worker.tar.gz", ("GET",)),
            ("/api/runtime/engine-onboarding", ("GET",)),
            ("/api/runtime/scene-tags", ("GET",)),
        }
        for path, methods in expected:
            assert (path, methods) in paths, f"runtime_view missing {path}"

    def test_runtime_view_has_user_facing_put_endpoints(self):
        from src.api.routes import runtime_view
        paths = {(r.path, tuple(sorted(r.methods))) for r in runtime_view.view_router.routes}
        for path in ("/api/runtime/runtimes/{runtime_id}/tags",
                     "/api/runtime/runtimes/{runtime_id}/max-concurrent",
                     "/api/runtime/runtimes/{runtime_id}/scene-tags",
                     "/api/runtime/runtimes/{runtime_id}/alias"):
            assert (path, ("PUT",)) in paths, f"runtime_view missing PUT {path}"

    def test_runtime_view_excludes_worker_protocol_endpoints(self):
        """Worker 协议 POST 端点不在 runtime_view（归 runtime.py / C 进程）。"""
        from src.api.routes import runtime_view
        paths = {r.path for r in runtime_view.view_router.routes}
        assert "/api/runtime/register" not in paths
        assert "/api/runtime/heartbeat" not in paths
        for p in paths:
            assert not p.endswith("/tasks/claim") and "claim-by-tag" not in p, \
                f"worker claim endpoints must stay in runtime.py: {p}"

    def test_runtime_view_registers_original_functions(self):
        """视图端点复用 runtime.py 原函数对象（单一实现源）。"""
        from src.api.routes import runtime_view, runtime
        by_path = {r.path: r.endpoint for r in runtime_view.view_router.routes}
        for path, fn_name in (
            ("/api/runtime/admin/machine-health", "machine_health_overview"),
            ("/api/runtime/tasks/{task_id}/status", "get_task_status"),
            ("/api/runtime/runtimes/{runtime_id}/tags", "set_runtime_tags"),
        ):
            if path in by_path:
                assert by_path[path] is getattr(runtime, fn_name), \
                    f"{path} must reuse runtime.{fn_name}"

    def test_a_process_includes_runtime_view_router(self):
        from src.api import grape_server
        from src.api.routes import runtime_view
        # include_router copies route objects, so compare (path, methods, endpoint)
        view = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()))),
                 id(getattr(r, "endpoint", None))) for r in runtime_view.view_router.routes}
        included = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()))),
                     id(getattr(r, "endpoint", None))) for r in _iter_app_routes(grape_server.app)}
        assert view & included, "A process must include runtime_view router routes"

    def test_a_process_excludes_worker_protocol_endpoints(self):
        """A 进程不含 Worker 协议端点（register/heartbeat/claim 归 C 进程）。"""
        from src.api import grape_server
        a = {(getattr(r, "path", ""), m) for r in _iter_app_routes(grape_server.app)
             for m in getattr(r, "methods", ())}
        for path in ("/api/runtime/register", "/api/runtime/heartbeat",
                     "/api/runtime/tasks/claim"):
            assert (path, "POST") not in a, f"A must not serve worker protocol {path}"


# ---------------------------------------------------- P1: events ops_router

class TestEventsOpsRouter:
    def test_events_module_exposes_ops_router(self):
        from src.api.routes import events
        assert events.ops_router is not None
        assert events.router is not None  # 单进程模式完整 router 保留

    def test_ops_router_contains_operation_endpoints(self):
        from src.api.routes import events
        got = {(r.path, tuple(sorted(r.methods))) for r in events.ops_router.routes}
        expected = {
            ("/api/events/{seq}/terminate", ("POST",)),
            ("/api/events/{seq}/redispatch", ("POST",)),
            ("/api/events/{seq}/a2a-redo", ("POST",)),
            ("/api/events/{seq}/chat", ("POST",)),
            ("/api/events/{seq}/chat/stream", ("POST",)),
            ("/api/events/{seq}/chat/submit", ("POST",)),
            ("/api/events/{seq}/chat", ("DELETE",)),
            ("/api/events/{seq}/knowledge", ("POST",)),
            ("/api/events/{seq}/flow/reset", ("POST",)),
        }
        for path, methods in expected:
            assert (path, methods) in got, f"ops_router missing {methods} {path}"

    def test_ops_router_excludes_view_endpoints(self):
        """GET 查看端点不在 ops_router（归 A 的完整 router）。"""
        from src.api.routes import events
        for r in events.ops_router.routes:
            assert "GET" not in r.methods, f"ops_router must not serve GET {r.path}"

    def test_ops_router_reuses_original_functions(self):
        from src.api.routes import events
        by_path = {r.path: r.endpoint for r in events.ops_router.routes}
        assert by_path.get("/api/events/{seq}/terminate") is events.terminate_event
        assert by_path.get("/api/events/{seq}/redispatch") is events.redispatch_event

    def test_full_router_still_has_both_view_and_ops(self):
        """单进程完整 router 同时含 GET 详情与操作端点（兼容不变）。"""
        from src.api.routes import events
        full = {(r.path, tuple(sorted(r.methods))) for r in events.router.routes}
        assert ("/api/events/detail/{seq}", ("GET",)) in full
        assert ("/api/events/{seq}/terminate", ("POST",)) in full

    def test_b_process_includes_ops_router_not_full(self):
        from src.api import grape_event_server
        from src.api.routes import events
        # include_router copies route objects, so compare (path, methods, endpoint)
        def _route_ids(routes):
            return {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()))),
                     id(getattr(r, "endpoint", None))) for r in routes}
        ops_ids = _route_ids(events.ops_router.routes)
        full_ids = _route_ids(events.router.routes)
        included = _route_ids(_iter_app_routes(grape_event_server.app))
        assert ops_ids & included, "B process must include events ops_router routes"
        # B 不加载完整 router（含 GET 查看）；ops 与 full 无交集即可判定
        assert not (full_ids - ops_ids) & included, \
            "B process must not include events view routes (GET detail etc.)"


# ------------------------------------------------- P1: nginx + systemd files

class TestNginxConfig:
    NGINX = REPO_ROOT / "nginx-grape.conf"

    def test_nginx_file_exists(self):
        assert self.NGINX.exists()

    def test_nginx_routes_three_upstreams(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        for port in ("38080", "38081", "38082"):
            assert f"127.0.0.1:{port}" in conf, f"nginx missing upstream {port}"

    def test_nginx_webhook_to_b(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        assert "location /webhook/" in conf
        # /webhook/ 块应指向 38081（B）
        m = re.search(r"location /webhook/ \{[^}]*proxy_pass http://127\.0\.0\.1:(\d+);", conf, re.S)
        assert m and m.group(1) == "38081", "webhook must route to B (38081)"

    def test_nginx_a2a_to_b(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        m = re.search(r"location /api/a2a \{[^}]*proxy_pass http://127\.0\.0\.1:(\d+);", conf, re.S)
        assert m and m.group(1) == "38081", "a2a must route to B (38081)"

    def test_nginx_events_ops_to_b(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        m = re.search(
            r"location ~ \^/api/events/.*terminate.*\{",
            conf)
        assert m, "nginx missing events ops location block"
        block = conf[m.end():m.end() + 400]
        pm = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+);", block)
        assert pm and pm.group(1) == "38081", "events ops must route to B (38081)"

    def test_nginx_runtime_worker_protocol_to_c(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        m = re.search(
            r"location ~ \^/api/runtime/.*register.*heartbeat.*\{",
            conf)
        assert m, "nginx missing runtime worker protocol location block"
        block = conf[m.end():m.end() + 600]
        pm = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+);", block)
        assert pm and pm.group(1) == "38082", "runtime worker protocol must route to C (38082)"

    def test_nginx_tasks_report_to_c(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        m = re.search(
            r"location ~ \^/api/tasks/\(notify\|complete[^)]*\) \{(.*?)\}",
            conf, re.S)
        assert m, "nginx missing tasks report location block"
        pm = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+);", m.group(1))
        assert pm and pm.group(1) == "38082", "tasks report must route to C (38082)"

    def test_nginx_default_location_to_a(self):
        conf = self.NGINX.read_text(encoding="utf-8")
        m = re.search(r"location / \{[^}]*proxy_pass http://127\.0\.0\.1:(\d+);", conf, re.S)
        assert m and m.group(1) == "38080", "default location must route to A (38080)"


class TestSystemdUnits:
    FILES = [
        (REPO_ROOT / "deploy/systemd/grape-server.service", "src.api.grape_server:app", "38080"),
        (REPO_ROOT / "deploy/systemd/grape-event-server.service", "src.api.grape_event_server:app", "38081"),
        (REPO_ROOT / "deploy/systemd/grape-worker-server.service", "src.api.grape_worker_server:app", "38082"),
    ]

    def test_all_units_exist(self):
        for path, _, _ in self.FILES:
            assert path.exists(), f"missing systemd unit {path}"

    def test_units_reference_correct_entry_and_port(self):
        for path, entry, port in self.FILES:
            content = path.read_text(encoding="utf-8")
            assert entry in content, f"{path.name} missing entry {entry}"
            assert f"--port {port}" in content, f"{path.name} missing --port {port}"
            assert "Restart=always" in content, f"{path.name} missing Restart=always"

    def test_worker_unit_sets_local_claimer(self):
        content = (REPO_ROOT / "deploy/systemd/grape-worker-server.service").read_text(encoding="utf-8")
        assert "GRAPE_LOCAL_CLAIMER=1" in content
        assert "RUNTIME_MAX_CONCURRENT_TASKS" in content

    def test_units_set_process_role(self):
        roles = {"grape-server.service": "server",
                 "grape-event-server.service": "event",
                 "grape-worker-server.service": "worker"}
        for name, role in roles.items():
            content = (REPO_ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            assert f"GRAPE_PROCESS_ROLE={role}" in content, f"{name} missing role {role}"

    def test_worker_and_event_disable_obs_log_db(self):
        """§4.4：仅 A 进程落库可观测性日志（OBS_LOG_DB=1），B/C 关闭。"""
        for name in ("grape-event-server.service", "grape-worker-server.service"):
            content = (REPO_ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
            assert "OBS_LOG_DB=0" in content, f"{name} must set OBS_LOG_DB=0"


# ------------------------------------------------------- P2: sweep split

class TestSweepSplit:
    def test_cleanup_exports_split_symbols(self):
        from src.runtime import cleanup
        for name in ("sweep_events_once", "sweep_tasks_once",
                     "sweep_events_loop", "sweep_tasks_loop", "sweep_loop", "sweep_once"):
            assert hasattr(cleanup, name), f"cleanup missing {name}"

    def test_sweep_once_composition(self):
        """sweep_once（单进程兼容）= B 维度 + C 维度清理全集。"""
        import inspect
        from src.runtime import cleanup
        src_once = inspect.getsource(cleanup.sweep_once)
        assert "sweep_stuck_events_once" in src_once
        assert "sweep_stuck_agent_tasks_once" in src_once

    def test_sweep_events_once_scope(self):
        """sweep_events_once（B 维度）含事件回收，不含 agent_tasks 回收。"""
        import inspect
        from src.runtime import cleanup
        src_b = inspect.getsource(cleanup.sweep_events_once)
        assert "sweep_stuck_events_once" in src_b
        assert "purge_stale_instances" in src_b
        assert "sweep_stuck_agent_tasks_once" not in src_b, \
            "B sweep must not touch agent_tasks recycling (C dimension)"

    def test_sweep_tasks_once_scope(self):
        """sweep_tasks_once（C 维度）只回收悬空 agent_tasks。"""
        import inspect
        from src.runtime import cleanup
        src_c = inspect.getsource(cleanup.sweep_tasks_once)
        assert "sweep_stuck_agent_tasks_once" in src_c
        assert "sweep_stuck_events_once" not in src_c, \
            "C sweep must not touch events recycling (B dimension)"

    def test_sweep_loops_use_shared_interval(self):
        """两个 loop 保持 15min（_SWEEP_INTERVAL，SWEEPER_INTERVAL_SECONDS 可配）。"""
        import inspect
        from src.runtime import cleanup
        for loop in (cleanup.sweep_events_loop, cleanup.sweep_tasks_loop):
            assert asyncio.iscoroutinefunction(loop)
            src = inspect.getsource(loop)
            assert "_SWEEP_INTERVAL" in src

    def test_sweep_tasks_once_returns_int(self):
        from src.runtime import cleanup
        # 无 DB 任务时应安全返回 0（worker 端无表场景 try/except 兜底）
        result = cleanup.sweep_tasks_once()
        assert isinstance(result, int)
        assert result >= 0


# ------------------------------------------- P3: table_maintenance split

class TestTableMaintenanceSplit:
    def test_main_uses_two_lock_keys(self):
        """单进程模式 main.py 同样用两个独立锁 key（与 A/C 语义一致）。"""
        content = (REPO_ROOT / "src/api/main.py").read_text(encoding="utf-8")
        assert 'run_with_distributed_lock("table_maintenance_tasks"' in content
        assert 'run_with_distributed_lock("table_maintenance_messages"' in content
        assert 'run_with_distributed_lock("table_maintenance"' not in content, \
            "legacy single lock key must be replaced by split keys"

    def test_main_agent_tasks_cleanup_under_tasks_lock(self):
        content = (REPO_ROOT / "src/api/main.py").read_text(encoding="utf-8")
        m = re.search(
            r'async with run_with_distributed_lock\("table_maintenance_tasks"[^)]*\):\s*'
            r'[^}]*?cleanup_old_agent_tasks_fields\(\)\s*[^}]*?delete_old_agent_tasks\(\)',
            content, re.S)
        assert m, "agent_tasks cleanup must run under table_maintenance_tasks lock"

    def test_a_entry_messages_lock_only(self):
        from src.api import grape_server
        src = Path(grape_server.__file__).read_text(encoding="utf-8")
        m = re.search(
            r'async with run_with_distributed_lock\("table_maintenance_messages"[^)]*\):\s*'
            r'[^}]*?cleanup_old_task_messages\(\)', src, re.S)
        assert m, "A entry: cleanup_old_task_messages must run under messages lock"
        assert "cleanup_old_agent_tasks_fields" not in src
        assert "delete_old_agent_tasks" not in src

    def test_c_entry_tasks_lock_only(self):
        from src.api import grape_worker_server
        src = Path(grape_worker_server.__file__).read_text(encoding="utf-8")
        m = re.search(
            r'async with run_with_distributed_lock\("table_maintenance_tasks"[^)]*\):\s*'
            r'[^}]*?cleanup_old_agent_tasks_fields\(\)\s*[^}]*?delete_old_agent_tasks\(\)',
            src, re.S)
        assert m, "C entry: agent_tasks cleanup must run under tasks lock"
        assert "cleanup_old_task_messages" not in src


# ------------------------------------------- P3: scheduled-task ownership

class TestScheduledTaskOwnership:
    """§6 分配表：A/B/C 各自的定时任务互不越界。"""

    def _entry_src(self, mod_name):
        mod = __import__(f"src.api.{mod_name}", fromlist=["x"])
        return Path(mod.__file__).read_text(encoding="utf-8")

    # --- A 归属（§6：lease sweep / knowledge / skill sync / feedback /
    #     upload cleanup / cooldown / server metrics / obs loops /
    #     master logging / knowledge migrate / D32）---
    def test_a_starts_user_facing_schedulers(self):
        src = self._entry_src("grape_server")
        # A2：legacy bot lease sweep 已下线，A 改为启动新 IM 引擎（start_im_engine）
        for marker in ("start_im_engine", "knowledge_sync_loop", "knowledge_write_worker",
                       "start_skill_sync_scheduler", "start_feedback_reconcile_scheduler",
                       "start_upload_cleanup_scheduler", "start_cooldown_scheduler",
                       "_start_server_metrics_task", "_start_obs_alert_task",
                       "_start_obs_rollup_task", "_start_obs_sample_task",
                       "_start_active_user_tracker_task", "install_master_logging",
                       "_migrate_knowledge", "migrate_user_accounts_to_grape"):
            assert marker in src, f"A entry missing A-owned task: {marker}"

    def test_a_does_not_start_worker_schedulers(self):
        src = self._entry_src("grape_server")
        for marker in ("start_local_claimer", "start_health_refresh_scheduler",
                       "reap_stale_runtimes", "start_bot_token_health", "sweep_tasks_loop"):
            assert marker not in src, f"A entry must not start C/B-owned task: {marker}"

    def test_b_starts_event_schedulers(self):
        src = self._entry_src("grape_event_server")
        assert "sweep_events_loop" in src
        assert "start_bot_token_health" in src

    def test_b_does_not_start_a_or_c_schedulers(self):
        src = self._entry_src("grape_event_server")
        for marker in ("start_local_claimer", "start_scheduler", "knowledge_write_worker",
                       "start_cooldown_scheduler", "_start_obs_alert_task"):
            assert marker not in src, f"B entry must not start A/C-owned task: {marker}"

    def test_c_starts_worker_schedulers(self):
        src = self._entry_src("grape_worker_server")
        for marker in ("start_local_claimer", "start_scheduler",
                       "start_health_refresh_scheduler", "probe_local_engines",
                       "reap_stale_runtimes", "sweep_tasks_loop"):
            assert marker in src, f"C entry missing C-owned task: {marker}"

    def test_c_does_not_start_a_schedulers(self):
        src = self._entry_src("grape_worker_server")
        for marker in ("knowledge_sync_loop", "knowledge_write_worker",
                       "start_cooldown_scheduler", "install_master_logging",
                       "_start_obs_alert_task", "start_skill_sync_scheduler"):
            assert marker not in src, f"C entry must not start A-owned task: {marker}"

    def test_main_single_process_starts_everything(self):
        """单进程兼容模式 main.py 保留全部定时任务（A+B+C 全集）。"""
        content = (REPO_ROOT / "src/api/main.py").read_text(encoding="utf-8")
        for marker in ("start_im_engine", "knowledge_sync_loop", "start_skill_sync_scheduler",
                       "start_upload_cleanup_scheduler", "start_cooldown_scheduler",
                       "start_bot_token_health", "start_local_claimer", "start_scheduler",
                       "start_health_refresh_scheduler", "probe_local_engines",
                       "reap_stale_runtimes", "_start_obs_alert_task",
                       "_start_active_user_tracker_task", "_start_server_metrics_task"):
            assert marker in content, f"main.py missing single-process task: {marker}"


# ------------------------------------------- 共享模块复用（middleware/health/spa）

class TestSharedModules:
    def test_middleware_module_exposes_installer(self):
        from src.api.middleware import install_request_middleware
        assert callable(install_request_middleware)

    def test_health_endpoints_module_exposes_installer(self):
        from src.api.health_endpoints import install_health_endpoints
        assert callable(install_health_endpoints)

    def test_spa_module_exposes_installer(self):
        from src.api.spa import install_spa_routes
        assert callable(install_spa_routes)

    def test_exception_handlers_module_exposes_installer(self):
        from src.api.exception_handlers import install_exception_handlers
        assert callable(install_exception_handlers)

    def test_all_three_entries_install_shared_modules(self):
        for mod_name in ("grape_server", "grape_event_server", "grape_worker_server"):
            src = self._src(mod_name)
            assert "install_request_middleware" in src, f"{mod_name} missing middleware"
            assert "install_exception_handlers" in src, f"{mod_name} missing exception handlers"
            assert "install_health_endpoints" in src, f"{mod_name} missing health endpoints"

    def _src(self, mod_name):
        mod = __import__(f"src.api.{mod_name}", fromlist=["x"])
        return Path(mod.__file__).read_text(encoding="utf-8")

    def test_health_service_names_distinct(self):
        """§4.7：三进程 /health 返回不同 service 标识。"""
        from src.api import grape_server, grape_event_server, grape_worker_server
        for mod, expect in ((grape_server, "server"),
                            (grape_event_server, "event-server"),
                            (grape_worker_server, "worker-server")):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert f'service_name="{expect}"' in src, f"{mod.__name__} health service name mismatch"

    def test_legacy_bot_manager_registry_retired(self):
        """A2（2026-11-10）：legacy BotManager 桥模块已删除，不再有 registry 槽位。"""
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("src.api.bot_manager_registry")


# ------------------------------------------- IM 单一归属（§10.7）

class TestIMOwnership:
    def test_im_engine_only_in_a_and_main(self):
        """§10.7 + A2：新 IM 引擎只在 A 进程 / 单进程入口启动。"""
        for mod_name in ("grape_event_server", "grape_worker_server"):
            mod = __import__(f"src.api.{mod_name}", fromlist=["x"])
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "start_im_engine" not in src, f"{mod_name} must not start the IM engine"

    def test_a_starts_im_engine(self):
        from src.api import grape_server
        src = Path(grape_server.__file__).read_text(encoding="utf-8")
        assert "engine_wiring.start_im_engine" in src
        assert "bot_manager_registry" not in src, "A2: legacy registry must be gone from the A entry"
        assert "import BotManager" not in src, "A2: A entry must not start the legacy BotManager"

    def test_main_starts_im_engine(self):
        content = (REPO_ROOT / "src/api/main.py").read_text(encoding="utf-8")
        assert "engine_wiring.start_im_engine" in content
        assert "set_bot_manager" not in content


# -------------------------- P1-P3 审查修复（A2 后：legacy 调用点已清零 + 状态驱动取消）

class TestReviewFixLegacyBotManagerRemoved:
    """A2（2026-11-10）：多进程 bot 动态增删链路整体下线，路由文件不得再引用 legacy 桥。"""

    ROUTE_FILES = [
        "src/api/routes/feishu_card.py",
        "src/api/routes/feishu_onboard.py",
        "src/api/routes/chat.py",
        "src/api/routes/agents.py",
    ]

    def test_no_route_file_references_legacy_bot_manager(self):
        for rel in self.ROUTE_FILES:
            content = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "bot_manager_registry" not in content, f"{rel} still imports the legacy registry"
            assert "from src.api.main import bot_manager" not in content, f"{rel} still imports bot_manager"


class TestReviewFixDirectTerminateStateDriven:
    """审查 Required #2：direct 模式跨进程终止改为状态驱动取消（C 侧探测杀引擎）。"""

    SRC = REPO_ROOT / "src/runtime/direct_executor.py"
    EVENTS = REPO_ROOT / "src/api/routes/events.py"

    def test_direct_executor_has_external_terminal_probe(self):
        src = self.SRC.read_text(encoding="utf-8")
        assert "_external_terminal" in src, "C-side streaming loop must probe external terminal state"
        assert "agent_tasks" in src and "events" in src
        # 周期探测（而非每条事件查库）
        assert "_PROBE_INTERVAL" in src

    def test_direct_executor_kills_engine_on_terminal(self):
        src = self.SRC.read_text(encoding="utf-8")
        assert "_kill_stale_engine" in src, "probe hit must kill engine subprocess"
        # 探测命中后 break 中断执行流（终态回写幂等，不会翻转状态）
        assert "externally terminated" in src

    def test_terminate_event_documents_state_driven_cancel(self):
        src = self.EVENTS.read_text(encoding="utf-8")
        assert "状态驱动取消" in src, "terminate_event must document state-driven cancel for multi-process"
        # cancel_direct_task 保留（单进程模式兜底）
        assert "cancel_direct_task" in src
