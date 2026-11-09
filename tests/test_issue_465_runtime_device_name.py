"""Issue #465：引擎驾驶舱「设备与引擎」runtimes 表「所属设备」列与 Workers 卡片名称不一致。

根因：dashboard ops/overview 构造 runtimes 行时，远端引擎 device_name 直接取
rc.device_name（边端自报 OS 主机名）；而 Agent 运行时页 Workers 卡片标题取
凭据自定义名 rc.name。期望：远端引擎优先 rc.name，为空回退 rc.device_name；
本机直连引擎（credential_id=0）沿用 host_name 逻辑不变。

测试 seam：get_ops_overview(repo="", user=...) 返回的 runtimes.list[].device_name。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _insert_credential(name: str, device_name: str) -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status, last_seen_at) "
            "VALUES (?, ?, ?, 'global', '', 'active', ?)",
            (f"hash_{uuid.uuid4().hex[:8]}", name, device_name, _now_iso()))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, host_name: str = "") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, scope, last_seen_at, host_name) "
            "VALUES (?, ?, 'online', 1, 'global', ?, ?)",
            (cid, f"engine-{uuid.uuid4().hex[:8]}", _now_iso(), host_name))
        rid = c.lastrowid
        db.commit()
    return rid


def _cleanup(cid: int, rids):
    with db_lock:
        c = db.cursor()
        for rid in rids:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        if cid:
            c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _overview_runtimes():
    from src.api.routes.dashboard import get_ops_overview
    user = {"id": 1, "username": "tester", "org_id": 0}
    data = get_ops_overview(repo="", user=user)
    return {rt["id"]: rt for rt in data["runtimes"]["list"]}


class TestRuntimeDeviceNameFallback:
    """runtimes 行 device_name 取值（Issue #465）。"""

    def test_remote_runtime_prefers_credential_name(self):
        """远端引擎：device_name 取用户自定义凭据名 rc.name（与 Workers 卡片一致）。"""
        cid = _insert_credential(name="我的工作机", device_name="worker-os-hostname")
        rid = _insert_runtime(cid)
        try:
            rts = _overview_runtimes()
            assert rid in rts, "active 远端引擎应出现在 runtimes 表"
            assert rts[rid]["device_name"] == "我的工作机", \
                "远端引擎所属设备应优先显示凭据自定义名 rc.name，而非边端自报 device_name"
        finally:
            _cleanup(cid, [rid])

    def test_remote_runtime_falls_back_to_device_name_when_name_empty(self):
        """存量凭据 name 为空：下发空串（前端统一占位「未命名设备」），不回退 device_name（#701 收口）。"""
        cid = _insert_credential(name="", device_name="legacy-os-host")
        rid = _insert_runtime(cid)
        try:
            rts = _overview_runtimes()
            assert rid in rts
            assert rts[rid]["device_name"] == "", \
                "rc.name 为空时后端应下发空值，由前端统一占位，不得回退 rc.device_name（Issue #701）"
        finally:
            _cleanup(cid, [rid])

    def test_local_runtime_uses_host_name(self):
        """本机直连引擎（credential_id=0）：device_name 沿用 host_name（回归不变）。"""
        rid = _insert_runtime(0, host_name="ctrl-host-465")
        try:
            rts = _overview_runtimes()
            assert rid in rts, "本机直连引擎应出现在 runtimes 表"
            assert rts[rid]["device_name"] == "ctrl-host-465", \
                "本机直连引擎 device_name 应取 host_name，本 Issue 不改变该逻辑"
        finally:
            _cleanup(0, [rid])
