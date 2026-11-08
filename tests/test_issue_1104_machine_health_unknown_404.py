# -*- coding: utf-8 -*-
"""Issue #1104：machine-health 详情端点对不存在 runtime_id 返回 404（契约 AT-RNT-52）。

现状（bug）：GET /api/runtime/admin/machine-health/{runtime_id} 对不存在的
runtime_id 走 get_health_score 现算通路，返回 200 + 全量详情结构，详情语义
未生效。契约原子 AT-RNT-52 期望未知 runtime_id → 404（统一错误结构）。

验证点：
1. 不存在 runtime_id → 404，detail 为 {"code": 404, "message": "runtime not found"}
2. 存在 runtime_id → 200，返回该 runtime 的详情结构（不回归）
"""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("GRAPE_DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".tmp_issue1104.db"))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.api.main import app
from src.session_manager import db, db_lock

_MOCK_USER = {"id": 1, "username": "issue1104", "auth_type": "api_key"}


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user_or_api_key] = lambda: dict(_MOCK_USER)
    app.dependency_overrides[require_no_session_token] = lambda: dict(_MOCK_USER)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'issue1104-rt-%'")
        c.execute("INSERT INTO agent_runtimes (name, credential_id, status) "
                  "VALUES ('issue1104-rt-exists', 0, 'online')")
        db.commit()
    yield TestClient(app)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'issue1104-rt-%'")
        db.commit()
    app.dependency_overrides.clear()


def _existing_runtime_id() -> int:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM agent_runtimes WHERE name='issue1104-rt-exists'")
        return c.fetchone()[0]


class TestMachineHealthUnknownRuntime404:
    """契约原子 AT-RNT-52 对应回归（grape-test::test_machine_health_unknown_runtime_404）。"""

    def test_unknown_runtime_404(self, client):
        """不存在 runtime_id → 404 + 统一错误结构，不再回退全量列表。"""
        with patch("src.runtime.health_scorer.get_health_score") as m:
            r = client.get("/api/runtime/admin/machine-health/999999")
        assert r.status_code == 404, r.text
        assert m.await_count if hasattr(m, "await_count") else m.called is False
        body = r.json()
        assert body.get("detail", {}).get("code") == 404, body
        assert body.get("detail", {}).get("message") == "runtime not found", body

    def test_existing_runtime_detail_ok(self, client):
        """存在 runtime_id → 行为保持：200 + 详情结构（含 engine_name）。"""
        rid = _existing_runtime_id()
        r = client.get(f"/api/runtime/admin/machine-health/{rid}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("runtime_id") == rid, data
        assert data.get("engine_name"), data

    def test_zero_runtime_id_404(self, client):
        """runtime_id=0（无效值）同样 404，不现算空记录。"""
        r = client.get("/api/runtime/admin/machine-health/0")
        assert r.status_code == 404, r.text
