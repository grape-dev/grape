"""Issue #3757：运行实例路由重复注册 → operationId 唯一性失守（回归用例）。

背景：`src/api/grape_server.py`（mgmt 进程）曾同时生效两套用户面路由注册——
显式 include 清单与文件末尾 `for r in [...]: app.include_router(r)` 循环，交集 28 个
router → 456 个 operationId 各告警一次（912 行/次，Loki 双流 1824 行/小时）；另外
`include_selected(runtime.router, methods={"GET"})` 与 `runtime_view.view_router` 是
同一批 handler 的两次注册（15 条 GET 交集）。渲染出的 `paths` 是字典，同一
(method,path) 被折叠，因此原有契约门禁完全看不到这批重复。

覆盖：
1. mgmt app 的路由注册表 (method,path) 与 operationId 严格唯一（验收口径）；
2. `get_openapi` 渲染不再出现 `Duplicate Operation ID` 告警；
3. 合并时并集不丢端点（第一套独有的 categories/im/takeover/org_*，第二套独有的
   observability/runtime_view）；
4. 启动自检 `assert_route_registry_unique` 反向用例（注入重复 router 必须抛错）；
5. CI 门禁 `check-openapi-endpoints.py` 在 fastapi>=0.140 `_IncludedRouter` 惰性包装
   下仍能抓到重复（修复前该守卫对所有路由重复静默假绿）。
"""

import importlib
import importlib.util
import os
import sys
import warnings

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, FastAPI

from src.api import server_common
from src.api.openapi import _iter_routes


def _ops(app):
    """展开 app.routes（含 `_IncludedRouter` 惰性包装）后的 (METHOD, path) 列表。"""
    out = []
    for route in app.routes:
        for r in _iter_routes(route):
            for m in (getattr(r, "methods", None) or set()):
                out.append((str(m).upper(), getattr(r, "path", None)))
    return out


@pytest.fixture(scope="module")
def mgmt_app():
    os.environ.setdefault("GRAPE_AUTO_DETECT_URL", "false")
    return importlib.import_module("src.api.grape_server").app


def _gate_script():
    """按文件路径加载 CI 门禁脚本（不作为包的一部分导入）。"""
    path = os.path.join(ROOT, "scripts", "check-openapi-endpoints.py")
    spec = importlib.util.spec_from_file_location("check_openapi_endpoints_3757", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. 验收：mgmt 进程路由注册表唯一
# ---------------------------------------------------------------------------

class TestMgmtRouteRegistryUnique:
    def test_no_duplicate_route_registration(self, mgmt_app):
        """`len(app.routes)` 展开后与按 (method,path) 去重的数量一致。"""
        ops = _ops(mgmt_app)
        dup = sorted({o for o in ops if ops.count(o) > 1})
        assert dup == [], f"duplicate route registrations remain: {dup}"
        assert len(ops) == len(set(ops))

    def test_contract_surface_operation_ids_unique(self, mgmt_app):
        """契约面（/api/ 且 include_in_schema）operationId 严格唯一。"""
        ids = []
        for route in mgmt_app.routes:
            for r in _iter_routes(route):
                if getattr(r, "include_in_schema", True) is False:
                    continue
                path = getattr(r, "path", "") or ""
                if not str(path).startswith("/api/"):
                    continue
                oid = getattr(r, "operation_id", None)
                if oid:
                    ids.append(oid)
        dup = sorted({i for i in ids if ids.count(i) > 1})
        assert dup == [], f"duplicate operationId remains: {dup[:10]}"

    def test_no_duplicate_operation_id_warning_on_render(self, mgmt_app):
        """渲染 schema 不再产生 `Duplicate Operation ID`（Issue 的原始现象）。"""
        from fastapi.openapi.utils import get_openapi

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            schema = get_openapi(title="grape-mgmt", version="test",
                                 routes=mgmt_app.routes)
        dup = [str(w.message) for w in caught if "Duplicate Operation ID" in str(w.message)]
        assert dup == [], f"schema render still warns: {dup[:5]}"
        ops = [(m, p) for p, methods in (schema.get("paths") or {}).items()
               for m in methods]
        assert len(ops) == len(set(ops))

    def test_servers_contract_surface_matches_route_table(self, mgmt_app):
        """启动自检实现与用例口径一致：同一 app 上不应报告冲突。"""
        assert server_common.route_registry_conflicts(mgmt_app) == []


# ---------------------------------------------------------------------------
# 2. 合并注册清单后不丢端点（并集完整性）
# ---------------------------------------------------------------------------

class TestUnionRegistrationKeepsEndpoints:
    @pytest.mark.parametrize("method,path", [
        # 第二套注册（已删除的循环）独有的两个 router —— 合并时必须并入清单
        ("GET", "/api/obs/overview"),
        ("GET", "/api/runtime/install"),
        # 第一套注册独有：直接删循环会丢的 9 个 router
        ("GET", "/api/categories/tree"),
        ("GET", "/api/im/status"),
        ("GET", "/api/takeover/sessions"),
        # runtime.router 的 GET 独有端点（view_router 未覆盖，仍须由 include_selected 补挂）
        ("GET", "/api/runtime/tasks/{task_id}/state"),
        ("PUT", "/api/runtime/runtimes/{runtime_id}/tags"),
    ])
    def test_endpoint_still_registered(self, mgmt_app, method, path):
        assert (method, path) in set(_ops(mgmt_app)), f"{method} {path} lost after merge"


# ---------------------------------------------------------------------------
# 3. 启动自检 fail-fast（反向用例：门禁不能是空断言）
# ---------------------------------------------------------------------------

def _router(path: str, operation_id: str = None, name: str = "handler"):
    router = APIRouter()

    @router.get(path, operation_id=operation_id, name=name)
    async def _h():  # noqa: D401 - 测试用最小 handler
        return {}

    return router


class TestStartupAssertionFailFast:
    def test_clean_app_passes(self):
        app = FastAPI()
        app.include_router(_router("/api/x/y", "x_y"))
        assert server_common.route_registry_conflicts(app) == []
        server_common.assert_route_registry_unique(app)  # 不抛

    def test_same_router_included_twice_raises(self):
        app = FastAPI()
        router = _router("/api/x/dup")
        app.include_router(router)
        app.include_router(router)  # 第二套注册的最小复现
        with pytest.raises(server_common.RouteRegistryConflict) as ei:
            server_common.assert_route_registry_unique(app)
        assert "/api/x/dup" in str(ei.value)

    def test_duplicate_operation_id_on_distinct_paths_raises(self):
        app = FastAPI()
        app.include_router(_router("/api/x/a", "same_id", "a"))
        app.include_router(_router("/api/x/b", "same_id", "b"))
        with pytest.raises(server_common.RouteRegistryConflict) as ei:
            server_common.assert_route_registry_unique(app)
        assert "same_id" in str(ei.value)

    def test_grape_server_wires_startup_assertion(self):
        """源级守卫：自检必须真正接在 mgmt 入口装配末尾。"""
        src = open(os.path.join(ROOT, "src", "api", "grape_server.py"),
                   encoding="utf-8").read()
        assert "assert_route_registry_unique(app)" in src
        # 第二套注册循环不得回流
        assert "for r in [" not in src


# ---------------------------------------------------------------------------
# 4. CI 门禁：fastapi>=0.140 `_IncludedRouter` 惰性包装下仍能抓重复
# ---------------------------------------------------------------------------

class TestCiGateSeesIncludedRouterWrappers:
    def test_gate_flags_duplicate_router(self):
        gate = _gate_script()
        app = FastAPI()
        router = _router("/api/x/dup")
        app.include_router(router)
        app.include_router(router)
        # fastapi>=0.140：app.routes 里是 _IncludedRouter，直接读 r.path 取不到值
        errors = gate.check_route_registration_unique(app.routes)
        assert any("/api/x/dup" in e for e in errors), errors

    def test_gate_flags_duplicate_operation_id(self):
        gate = _gate_script()
        app = FastAPI()
        app.include_router(_router("/api/x/a", "same_id", "a"))
        app.include_router(_router("/api/x/b", "same_id", "b"))
        errors = gate.check_app_operation_ids_unique(app.routes, "synthetic")
        assert any("same_id" in e for e in errors), errors

    def test_gate_covers_all_process_entries(self):
        """门禁必须逐个进程入口扫（此前只扫单进程 main → mgmt 长期假绿）。"""
        gate = _gate_script()
        apps = {}
        for name in ("grape_server", "grape_event_server",
                     "grape_worker_server", "grape_insight_server"):
            apps[name] = importlib.import_module(f"src.api.{name}").app
        assert gate.check_process_apps_route_registry(apps) == []


# ---------------------------------------------------------------------------
# 5. include_selected 的 exclude_ops（runtime_view 与 runtime GET 子集去重）
# ---------------------------------------------------------------------------

class TestIncludeSelectedExcludeOps:
    def test_exclude_ops_skips_covered_pairs(self):
        app = FastAPI()
        router = _merge(_router("/api/x/keep"), _router("/api/x/skip"))
        server_common.include_selected(app, router, methods={"GET"},
                                       exclude_ops={("GET", "/api/x/skip")})
        paths = {p for _, p in _ops(app)}
        assert "/api/x/keep" in paths
        assert "/api/x/skip" not in paths


def _merge(*routers):
    """把多个 APIRouter 的路由挂到同一个 router 上（测试辅助）。"""
    merged = APIRouter()
    for r in routers:
        for route in r.routes:
            merged.routes.append(route)
    return merged