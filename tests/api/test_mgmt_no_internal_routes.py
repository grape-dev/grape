"""mgmt 作为管理态服务，不挂载业务型 /internal/* 契约（架构约定，2026-11）。

背景：mgmt 曾挂载 src/domains/mgmt/routes/internal.py（webhook-config / sessions /
scenes / engine-ids / tokens-verify / bot-config / skills）。实测 24h 零流量，
且 event/worker/insight 对 mgmt 的数据依赖都是直读共享库或进程内契约。

约定：管理面不被业务面 HTTP 反向调用；只有 runtime/insight 保留业务型 internal 路由。
GSB 注册端点（/internal/gsb/*）不受影响。
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


def _drop(*names):
    for n in names:
        sys.modules.pop(n, None)


def test_mgmt_does_not_import_or_mount_business_internal_router():
    _drop(MGMT_INTERNAL, RUNTIME_INTERNAL, INSIGHT_INTERNAL)
    attach_internal_routes(FastAPI(), "mgmt")
    assert MGMT_INTERNAL not in sys.modules, "mgmt 不应再挂载业务型 /internal/* 契约"


def test_runtime_still_mounts_its_internal_router():
    _drop(RUNTIME_INTERNAL)
    attach_internal_routes(FastAPI(), "runtime")
    assert RUNTIME_INTERNAL in sys.modules, "runtime 的 /internal/* 必须保留（mgmt 依赖）"
    mod = importlib.import_module(RUNTIME_INTERNAL)
    paths = [r.path for r in mod.router.routes]
    assert any(p.startswith("/internal") for p in paths), paths


def test_insight_still_mounts_its_internal_router():
    _drop(INSIGHT_INTERNAL)
    attach_internal_routes(FastAPI(), "insight")
    assert INSIGHT_INTERNAL in sys.modules, "insight 的 /internal/* 必须保留（mgmt 依赖）"
    mod = importlib.import_module(INSIGHT_INTERNAL)
    paths = [r.path for r in mod.router.routes]
    assert any(p.startswith("/internal") for p in paths), paths

def test_internal_guard_404s_json_and_keeps_registered_routes():
    """未知 /internal/* 必须是 JSON 404，且不能吞掉先注册的 GSB 路由。"""
    from fastapi import FastAPI
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
