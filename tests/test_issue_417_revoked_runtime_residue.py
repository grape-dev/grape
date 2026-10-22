"""Issue #417：可观测性「设备与引擎」页 runtime 表已吊销设备仍残留显示。

根因（见 docs/issue-417/spec.md）：
1. dashboard ops/overview 的 runtimes 查询只跳过 rc.status=='revoked'；
   吊销=物理删除凭据行后 LEFT JOIN 得 NULL，孤儿 agent_runtimes 行继续显示。
2. runtime_register 鉴权在 db_lock 外，吊销与注册竞态会以失效 credential_id
   重新 INSERT agent_runtimes（复活残留行）。

测试覆盖验收两场景（吊销后立即查询 / 刷新后再查询）与竞态、级联清理。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _insert_credential(name: str = "", status: str = "active", scope: str = "global") -> int:
    name = name or f"cred-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?)",
            (f"hash_{name}", name, f"dev-{name}", scope, status, _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, name: str = "", last_seen: str = "") -> int:
    name = name or f"engine-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, scope, last_seen_at) "
            "VALUES (?, ?, 'online', 1, 'global', ?)",
            (cid, name, last_seen or _now_iso()))
        rid = c.lastrowid
        db.commit()
    return rid, name


def _delete_credential_row(cid: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _cleanup(cid: int):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE credential_id=?)", (cid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _overview_runtime_ids():
    from src.api.routes.dashboard import get_ops_overview
    user = {"id": 1, "username": "tester", "org_id": 0}
    data = get_ops_overview(repo="", user=user)
    return {rt["id"] for rt in data["runtimes"]["list"]}


class _FakeRequest:
    headers = {}
    client = None


class TestRevokedRuntimeResidue:
    """吊销后 runtime 表（ops/overview runtimes.list）不应再出现该设备。"""

    def test_orphan_runtime_hidden_after_credential_deleted(self):
        """场景一（立即查询）：凭据行已物理删除、agent_runtimes 残留 → 不显示。"""
        cid = _insert_credential()
        rid, _ = _insert_runtime(cid)
        try:
            assert rid in _overview_runtime_ids()
            _delete_credential_row(cid)
            assert rid not in _overview_runtime_ids(), \
                "凭据已删除的孤儿引擎行不应出现在 runtime 表（查询侧兜底失效）"
        finally:
            _cleanup(cid)

    def test_revoked_status_runtime_hidden(self):
        """凭据 status='revoked'（软吊销历史行）→ 不显示（回归）。"""
        cid = _insert_credential(status="revoked")
        rid, _ = _insert_runtime(cid)
        try:
            assert rid not in _overview_runtime_ids()
        finally:
            _cleanup(cid)

    def test_active_runtime_still_visible(self):
        """active 凭据引擎正常显示（不误伤）。"""
        cid = _insert_credential()
        rid, _ = _insert_runtime(cid)
        try:
            assert rid in _overview_runtime_ids()
        finally:
            _cleanup(cid)

    def test_null_status_credential_runtime_still_visible(self):
        """凭据行存在但 status 为 NULL 的历史合法行 → 显示（与 instances.py 语义一致，不误伤）。"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, last_seen_at) "
                "VALUES (?, ?, ?, 'global', '', ?)",
                (f"hash_null_{uuid.uuid4().hex[:6]}", f"cred-null-{uuid.uuid4().hex[:4]}",
                 f"dev-null-{uuid.uuid4().hex[:4]}", _now_iso()))
            cid = c.lastrowid
            db.commit()
        rid, _ = _insert_runtime(cid)
        try:
            assert rid in _overview_runtime_ids()
        finally:
            _cleanup(cid)


class TestRegisterRaceGuard:
    """吊销与注册竞态：鉴权通过后凭据被删 → register 必须被拒，不产生新行。"""

    def test_register_after_revoke_rejected(self):
        from fastapi import HTTPException
        from src.api.routes.runtime import runtime_register, RuntimeRegBody

        cid = _insert_credential()
        try:
            body = RuntimeRegBody(device_name="race-dev", runtimes=[{"name": "race-engine"}])
            req = _FakeRequest()
            # 正常注册成功
            out = runtime_register(body, req, credential_id=cid)
            assert out["runtimes"]

            # 模拟竞态：鉴权已过（直接传 credential_id），凭据随后被吊销删除
            # （与 revoke 一致：凭据行 + 其 agent_runtimes 一并删除）
            with db_lock:
                c2 = db.cursor()
                c2.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cid,))
                c2.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
                db.commit()
            with pytest.raises(HTTPException) as ei:
                runtime_register(body, req, credential_id=cid)
            assert ei.value.status_code == 401

            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=?", (cid,))
                n = c.fetchone()[0]
            assert n == 0, "凭据吊销后 register 不得再写入 agent_runtimes（复活残留行）"
        finally:
            _cleanup(cid)


class TestRevokeCleansEngineCapabilities:
    """吊销级联清理 engine_capabilities（吊销事件同步清理补全）。"""

    def test_revoke_deletes_engine_capabilities(self):
        from src.api.routes.settings import revoke_runtime_credential

        cid = _insert_credential()
        rid, _ = _insert_runtime(cid)
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'coding', 1)", (rid,))
            db.commit()
        try:
            revoke_runtime_credential(cid, {"id": 1, "username": "tester", "auth_type": "api_key"})
            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=?", (rid,))
                assert c.fetchone()[0] == 0, "revoke 应级联删除 engine_capabilities"
                c.execute("SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=?", (cid,))
                assert c.fetchone()[0] == 0
        finally:
            _cleanup(cid)
