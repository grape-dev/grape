"""P2 服务接口补齐（N1–N13）契约与行为测试。

覆盖：
  · 契约面：RuntimeService / MgmtService 的 N1–N10 方法在 Local/Remote 两侧都已实现；
  · 挂载面：mgmt 只挂**只读白名单 + xwrite/apply**（方案 §D5，取代"mgmt 完全不开放"）；
  · 行为面：端点函数直调（绕过服务令牌）返回稳定结构；N5 拒绝未登记 namespace；
    N13 拒绝未登记 action 与缺失幂等键。
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402

from src.api.server_common import attach_internal_routes  # noqa: E402
from src.domains.contracts.local.mgmt_local import MgmtServiceLocal  # noqa: E402
from src.domains.contracts.local.runtime_local import RuntimeServiceLocal  # noqa: E402
from src.domains.contracts.mgmt_service import MgmtService  # noqa: E402
from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote  # noqa: E402
from src.domains.contracts.remote.runtime_remote import RuntimeServiceRemote  # noqa: E402
from src.domains.contracts.runtime_service import RuntimeService  # noqa: E402

# mgmt 只读白名单（新增条目必须改这里 + 评审；路径已归一化 {x:path} → {x}）
MGMT_READONLY_WHITELIST = {
    ("/internal/repos/{repo_key}/webhook-config", "GET"),
    ("/internal/repos/{repo_key}/bot-config", "GET"),
    ("/internal/sessions/{repo_key}/{agent_type}", "GET"),
    ("/internal/scenes/{repo_key}", "GET"),
    ("/internal/scenes/{scene_id}/engine-ids", "GET"),
    ("/internal/tokens/verify", "POST"),
    ("/internal/skills/{skill_id}", "GET"),
    ("/internal/accounts/{account_id}", "GET"),
    ("/internal/knowledge/{knowledge_id}", "GET"),
    ("/internal/user-mapping", "POST"),
    ("/internal/repos/{repo_key}", "GET"),
}
MGMT_XWRITE = {("/internal/xwrite/apply", "POST")}


def _norm(path: str) -> str:
    return path.replace(":path}", "}")


def _mount(domain: str) -> FastAPI:
    app = FastAPI()
    attach_internal_routes(app, domain)
    return app


def _routes(app: FastAPI):
    from src.api.openapi import _iter_routes

    out = set()
    for route in app.routes:
        for r in _iter_routes(route):
            for m in getattr(r, "methods", set()) or set():
                out.add((_norm(getattr(r, "path", "")), m))
    return out


def test_contract_methods_implemented_both_sides():
    runtime_methods = ("list_skills", "get_skill_detail", "list_engine_capabilities",
                       "get_runtime_credential_meta", "get_meta_namespace",
                       "get_tasks_batch", "get_task_stats")
    mgmt_methods = ("get_repo_bundle", "get_account", "get_knowledge",
                    "get_user_mapping_batch")
    for name in runtime_methods:
        assert name in RuntimeService.__abstractmethods__, f"RuntimeService 缺 {name}"
        assert hasattr(RuntimeServiceLocal, name), f"Local 缺 {name}"
        assert hasattr(RuntimeServiceRemote, name), f"Remote 缺 {name}"
    for name in mgmt_methods:
        assert name in MgmtService.__abstractmethods__, f"MgmtService 缺 {name}"
        assert hasattr(MgmtServiceLocal, name), f"Local 缺 {name}"
        assert hasattr(MgmtServiceRemote, name), f"Remote 缺 {name}"


def test_mgmt_mounts_only_readonly_whitelist_and_xwrite():
    routes = _routes(_mount("mgmt"))
    internal = {r for r in routes if r[0].startswith("/internal/")}
    allowed = MGMT_READONLY_WHITELIST | MGMT_XWRITE
    unexpected = internal - allowed
    assert not unexpected, f"mgmt 挂载了白名单外的 /internal 路由：{sorted(unexpected)}"
    assert MGMT_XWRITE <= internal, "N13 xwrite/apply 未挂载"


def test_runtime_mounts_p2_endpoints():
    routes = {p for p, _m in _routes(_mount("runtime"))}
    for path in ("/internal/skills", "/internal/skills/{skill_id}",
                 "/internal/engine-capabilities", "/internal/runtime-credentials/{runtime_id}",
                 "/internal/meta/{namespace}", "/internal/tasks/batch",
                 "/internal/stats/tasks", "/internal/xwrite/apply"):
        assert path in routes, f"runtime 缺 {path}"


def test_runtime_endpoints_behaviour():
    from src.domains.runtime.routes import internal as rt

    skills = asyncio.run(rt.list_skills(limit=5))
    assert isinstance(skills, list)
    caps = asyncio.run(rt.list_engine_capabilities(limit=5))
    assert isinstance(caps, list)
    assert asyncio.run(rt.get_tasks_batch({"task_ids": []})) == []
    stats = asyncio.run(rt.get_task_stats({"repo_key": "no/such-repo"}))
    assert isinstance(stats, dict) and "aggregate" in stats
    with_meta = asyncio.run(rt.get_meta_namespace("allow_signup"))
    assert isinstance(with_meta, dict)


def test_meta_namespace_rejects_unregistered():
    from src.domains.runtime.routes import internal as rt

    rejected = asyncio.run(rt.get_meta_namespace("gitcode_webhook_token"))
    assert rejected.get("status") == "rejected" or rejected == {}


def test_xwrite_apply_requires_key_and_registered_action():
    from src.domains.runtime.routes import internal as rt

    no_key = asyncio.run(rt.xwrite_apply({"action": "engine_cap_upsert", "payload": {}}))
    assert no_key["status"] == "rejected"
    unknown = asyncio.run(rt.xwrite_apply(
        {"action": "drop_table", "idempotency_key": "k1", "payload": {}}))
    assert unknown["status"] == "rejected"


def test_xwrite_apply_is_idempotent_for_registered_action():
    from src.domains.runtime.routes import internal as rt

    body = {"action": "engine_cap_upsert", "idempotency_key": "p2-test-engine-cap",
            "payload": {"rid": 987654, "tag": "p2-test", "priority": 10}}
    first = asyncio.run(rt.xwrite_apply(dict(body)))
    second = asyncio.run(rt.xwrite_apply(dict(body)))
    if first["status"] == "applied":
        assert second["status"] == "duplicate"
    else:
        # sqlite 测试库无 runtime 自有 DSN 时必须是可解释的失败/拒绝，而非静默成功
        assert first["status"] in ("rejected", "failed"), first


def test_mgmt_endpoints_behaviour():
    from src.domains.mgmt.routes import internal as mg

    assert asyncio.run(mg.get_repo_bundle("no/such-repo")) == {}
    assert asyncio.run(mg.get_account("no-such-account")) == {}
    assert asyncio.run(mg.get_knowledge(99999999)) == {}
    assert asyncio.run(mg.get_user_mapping_batch({"keys": []})) == []
    rejected = asyncio.run(mg.xwrite_apply({"action": "raw_sql", "idempotency_key": "k"}))
    assert rejected["status"] == "rejected"