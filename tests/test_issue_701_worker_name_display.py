"""Issue #701：worker 名称展示统一——后端展示名下发口径取 runtime_credentials.name。

rc.name 唯一来源，空则下发空值（前端统一占位「未命名设备」），不回退
rc.device_name / host_name。覆盖：
  - dashboard ops/overview runtimes 行 device_name（#465 修复处：回退逻辑移除）
  - task-logs /{id}/info device_name（原直接下发 rc.device_name）
  - events 事件详情 engine_device（原直接下发 rc.device_name）

测试 seam：各 API 出口字段的取值。
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


class TestWorkerNameDisplayUnification:
    """后端展示名下发口径（Issue #701）。"""

    def test_runtimes_device_name_prefers_credential_name(self):
        """远端引擎 device_name = rc.name（凭据自定义名）。"""
        cid = _insert_credential(name="我的工作机", device_name="worker-os-hostname")
        rid = _insert_runtime(cid)
        try:
            rts = _overview_runtimes()
            assert rid in rts
            assert rts[rid]["device_name"] == "我的工作机"
        finally:
            _cleanup(cid, [rid])

    def test_runtimes_empty_credential_name_yields_empty_string(self):
        """rc.name 为空：下发空串（前端占位），不回退 device_name（#701 收口）。"""
        cid = _insert_credential(name="", device_name="legacy-os-host")
        rid = _insert_runtime(cid)
        try:
            rts = _overview_runtimes()
            assert rid in rts
            assert rts[rid]["device_name"] == "", \
                "rc.name 为空时后端应下发空值，由前端统一占位，不得回退 rc.device_name"
        finally:
            _cleanup(cid, [rid])

    def test_runtimes_blank_credential_name_yields_empty_string(self):
        """rc.name 为纯空白：视同空名，下发空串。"""
        cid = _insert_credential(name="   ", device_name="blank-host")
        rid = _insert_runtime(cid)
        try:
            rts = _overview_runtimes()
            assert rid in rts
            assert rts[rid]["device_name"] == "", "空白 rc.name 应视同空名下发空值"
        finally:
            _cleanup(cid, [rid])

    def test_task_logs_info_device_name_uses_credential_name(self):
        """task-logs info：device_name 字段下发 rc.name（原为 rc.device_name）。"""
        from src.api.routes.task_logs import get_task_runtime_info
        cid = _insert_credential(name="任务执行机", device_name="task-os-host")
        rid = _insert_runtime(cid)
        task_id = f"t_{uuid.uuid4().hex[:8]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status, created_at) "
                "VALUES (?, 'sess-701', ?, '', 'completed', ?)",
                (task_id, rid, _now_iso()))
            db.commit()
        try:
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["device_name"] == "任务执行机", \
                "task info 的 device_name 应下发凭据自定义名 rc.name"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
                db.commit()
            _cleanup(cid, [rid])

    def test_task_logs_info_empty_credential_name_yields_empty_string(self):
        """task-logs info：rc.name 为空 → device_name 下发空串，不回退。"""
        from src.api.routes.task_logs import get_task_runtime_info
        cid = _insert_credential(name="", device_name="task-os-host")
        rid = _insert_runtime(cid)
        task_id = f"t_{uuid.uuid4().hex[:8]}"
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status, created_at) "
                "VALUES (?, 'sess-701', ?, '', 'completed', ?)",
                (task_id, rid, _now_iso()))
            db.commit()
        try:
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["device_name"] == "", \
                "rc.name 为空时 task info device_name 应为空串（前端占位），不回退 rc.device_name"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
                db.commit()
            _cleanup(cid, [rid])

    def test_event_detail_engine_device_uses_credential_name(self):
        """events 事件详情：engine_device 下发 rc.name（原为 rc.device_name）。"""
        from src.api.routes.events import get_event_detail
        cid = _insert_credential(name="事件执行机", device_name="evt-os-host")
        rid = _insert_runtime(cid)
        seq = None
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, event_type, title, actor, status, payload) "
                "VALUES ('gitcode:demo/repo701', 'issue', 'evt', 'tester', 'done', '{}')")
            seq = c.lastrowid
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, prompt, status, created_at) "
                "VALUES (?, 'sess-701', ?, ?, '', 'completed', ?)",
                (f"t_{uuid.uuid4().hex[:8]}", rid, seq, _now_iso()))
            db.commit()
        try:
            user = {"id": 1, "username": "tester", "org_id": 0, "auth_type": "api_key"}
            detail = get_event_detail(seq, user=user)
            assert detail.get("engine_device") == "事件执行机", \
                "事件详情 engine_device 应下发凭据自定义名 rc.name"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM events WHERE seq=?", (seq,))
                c.execute("DELETE FROM agent_tasks WHERE event_seq=?", (seq,))
                db.commit()
            _cleanup(cid, [rid])
