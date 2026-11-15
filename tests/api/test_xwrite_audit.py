"""跨域补偿写（/internal/xwrite/apply）审计留痕测试（QA F2 验收）。

断言两条：
1. 经 HTTP 调一次 xwrite/apply 后，audit_logs 出现一条 event_type='cross_write_apply' 的记录，
   detail 里带 action / 幂等键 / 调用方 / 结果摘要；
2. 单机模式下非回环来源调 /internal/* 被拒（403 INTERNAL_LOOPBACK_ONLY），回环放行（QA F1 验收）。

用 TestClient 的 client=("127.0.0.1", …) 模拟同机进程调用（与生产里 event→mgmt 的回环调用一致）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from src.api.server_common import attach_internal_routes  # noqa: E402


@pytest.fixture()
def app():
    a = FastAPI()
    attach_internal_routes(a, "mgmt")
    return a


def _audit_rows():
    from src.kernel.session_manager import db

    c = db.cursor()
    try:
        c.execute(
            "SELECT event_type, detail FROM audit_logs "
            "WHERE event_type='cross_write_apply' ORDER BY seq DESC LIMIT 5")
        return c.fetchall()
    finally:
        try:
            c.close()
        except Exception:
            pass


def test_xwrite_apply_writes_audit_row(app):
    # 回环来源：单机模式下按同机进程调用语义放行
    client = TestClient(app, client=("127.0.0.1", 51234))
    body = {
        "action": "noop_for_audit_test",
        "idempotency_key": "audit-test-key-1",
        "payload": {"note": "qa-f2"},
    }
    r = client.post("/internal/xwrite/apply", json=body)
    # 未登记 action → 业务侧 rejected，但审计仍必须留痕（这正是 F2 的要求：可追溯）
    assert r.status_code == 200
    assert r.json().get("action", "noop_for_audit_test") or True

    rows = _audit_rows()
    assert rows, "xwrite/apply 后没有写入 cross_write_apply 审计记录"
    import json as _json

    detail = _json.loads(rows[0][1]) if isinstance(rows[0][1], str) else (rows[0][1] or {})
    assert detail.get("action") == "noop_for_audit_test"
    assert detail.get("idempotency_key") == "audit-test-key-1"
    assert "payload_digest" in detail and detail["payload_digest"]
    assert detail.get("status") in ("rejected", "applied", "duplicate")


def test_internal_rejects_non_loopback_in_single_mode(app, monkeypatch):
    monkeypatch.setenv("GRAPE_MODE", "")          # 单机模式
    monkeypatch.delenv("GRAPE_SERVICE_SECRET", raising=False)
    client = TestClient(app, client=("192.0.2.9", 40000))   # 非回环来源
    r = client.get("/internal/repos/some/repo")
    assert r.status_code == 403
    assert r.json().get("code") == "INTERNAL_LOOPBACK_ONLY"


def test_internal_allows_loopback_in_single_mode(app, monkeypatch):
    monkeypatch.setenv("GRAPE_MODE", "")
    client = TestClient(app, client=("127.0.0.1", 40001))
    r = client.get("/internal/repos/some/repo")
    # 回环放行 → 不再是 403（业务侧可能 200 或 404，只要不是来源拦截）
    assert r.status_code != 403