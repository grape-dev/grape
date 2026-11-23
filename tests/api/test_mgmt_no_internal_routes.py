"""mgmt 内部端点挂载口径（P2/D5 修订版，2026-11-14）。

历史：2026-11 架构收敛期 mgmt 完全关闭业务型 `/internal/*`（24h 零流量 + 42 次 404
被 catch 降级）。三库独立化方案 §D5 修订为**只读白名单**：

  · 允许：webhook-config / bot-config / sessions / scenes / engine-ids / tokens-verify /
    skills / accounts / knowledge / user-mapping / repos 聚合（全部只读语义）
  · 允许：单一 POST `/internal/xwrite/apply`（N13 补偿应用，幂等键 + action 白名单）
  · 禁止：任何未登记路径、任何 SQL/表名透传

本测试锁死白名单；新增端点必须同时改
`tests/contracts/test_internal_contract_p2.py::MGMT_READONLY_WHITELIST` 并评审。
"""
import importlib
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402

from src.api.server_common import attach_internal_routes  # noqa: E402

MGMT_INTERNAL = "src.domains.mgmt.routes.internal"
RUNTIME_INTERNAL = "src.domains.runtime.routes.internal"
INSIGHT_INTERNAL = "src.domains.insight.routes.internal"

ALLOWED_MGMT_PATHS = {
    "/internal/repos/{repo_key:path}/webhook-config",
    "/internal/repos/{repo_key:path}/bot-config",
    "/internal/sessions/{repo_key:path}/{agent_type}",
    "/internal/scenes/{repo_key:path}",
    "/internal/scenes/{scene_id}/engine-ids",
    "/internal/tokens/verify",
    "/internal/skills/{skill_id}",
    "/internal/accounts/{account_id}",
    "/internal/knowledge/{knowledge_id}",
    "/internal/user-mapping",
    "/internal/platform-cards",
    "/internal/repos/{repo_key:path}",
    "/internal/xwrite/apply",
}


def _drop(*names):
    for n in names:
        sys.modules.pop(n, None)


def _mount_paths(app) -> set[str]:
    """展开 include_router 的惰性包装（fastapi>=0.140）后取路径。"""
    from src.api.openapi import _iter_routes

    out = set()
    for route in app.routes:
        for r in _iter_routes(route):
            out.add(getattr(r, "path", ""))
    return out


def test_mgmt_mounts_readonly_whitelist():
    _drop(MGMT_INTERNAL, RUNTIME_INTERNAL, INSIGHT_INTERNAL)
    app = FastAPI()
    attach_internal_routes(app, "mgmt")
    assert MGMT_INTERNAL in sys.modules, "mgmt 应挂载只读白名单 router"
    paths = {p for p in _mount_paths(app) if p.startswith("/internal")}
    assert paths, "mgmt 未挂载任何 /internal 路由"
    assert paths <= ALLOWED_MGMT_PATHS, f"白名单外：{sorted(paths - ALLOWED_MGMT_PATHS)}"


def test_mgmt_has_no_arbitrary_write_endpoints():
    _drop(MGMT_INTERNAL)
    mod = importlib.import_module(MGMT_INTERNAL)
    writes = []
    for r in mod.readonly_router.routes:
        methods = set(getattr(r, "methods", set()) or set()) - {"GET", "HEAD", "OPTIONS"}
        # 只允许两个只读语义的 POST：token 校验 / 批量映射查询
        if methods and r.path not in ("/internal/tokens/verify", "/internal/user-mapping"):
            writes.append((r.path, sorted(methods)))
    assert not writes, f"只读白名单出现写端点：{writes}"


def test_runtime_still_mounts_its_internal_router():
    _drop(RUNTIME_INTERNAL)
    attach_internal_routes(FastAPI(), "runtime")
    assert RUNTIME_INTERNAL in sys.modules, "runtime 的 /internal/* 必须保留"
    mod = importlib.import_module(RUNTIME_INTERNAL)
    paths = [r.path for r in mod.router.routes]
    assert any(p.startswith("/internal") for p in paths), paths


def test_insight_still_mounts_its_internal_router():
    _drop(INSIGHT_INTERNAL)
    attach_internal_routes(FastAPI(), "insight")
    assert INSIGHT_INTERNAL in sys.modules, "insight 的 /internal/* 必须保留"
    mod = importlib.import_module(INSIGHT_INTERNAL)
    paths = [r.path for r in mod.router.routes]
    assert any(p.startswith("/internal") for p in paths), paths


def test_internal_guard_404s_json_and_keeps_registered_routes():
    """未知 /internal/* 必须是 JSON 404，且不能吞掉先注册的 GSB 路由。"""
    from fastapi.testclient import TestClient

    from src.api.server_common import attach_internal_guard

    app = FastAPI()

    @app.get("/internal/gsb/services")
    async def gsb_services():
        return {"count": 1}

    attach_internal_guard(app)
    c = TestClient(app)
    assert c.get("/internal/gsb/services").status_code == 200
    r = c.get("/internal/repos/x/webhook-config")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == 404