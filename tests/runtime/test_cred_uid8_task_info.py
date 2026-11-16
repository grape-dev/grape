"""[spec 2026-11-14-eventview-cred-uid8-fix] task-logs info 沙箱引擎信息位契约守卫。

守卫对象（grape commit cbb62e7bb 引入，防功能丢失）：
  1. credential_uid8：runtime_credentials.credential_uid 前 8 位下发——沙箱 worker
     systemd 服务名/安装目录单锚（install-worker.sh:112-113 sandbox-worker-<uid8>）。
  2. sandbox_provider：云引擎四段名第 3 段派生（与 dashboard.py:1789 同口径）。
  3. 语义红线：credential_id 必须不在返回 dict（spec 拍板①——不留兼容层；
     防止后续改动"顺手"补回 id 字段）。

边界：
  - 普通引擎（单段名）→ sandbox_provider 为空串。
  - credential_uid 空串/旧行缺值 → uid8 为空串，不炸。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _insert_credential(cred_uid: str) -> int:
    """插入 runtime_credentials 并返回 id（cred_type=sandbox 形态）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, cred_type, platform, scope, status, owner_name, credential_uid) "
            "VALUES (?, ?, 'sandbox', 'hwcloud', 'user', 'active', 'tester', ?)",
            (f"hash_{uuid.uuid4().hex[:8]}", f"cred-uid8-{cred_uid[:8] or uuid.uuid4().hex[:6]}", cred_uid))
        cid = c.lastrowid
        db.commit()
    return cid


def _insert_runtime(cid: int, engine_name: str) -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, installed) "
            "VALUES (?, ?, 'online', 1, 1)",
            (cid, engine_name))
        rid = c.lastrowid
        db.commit()
    return rid


def _insert_task(rid: int) -> str:
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status, created_at) "
            "VALUES (?, 'sess-uid8', ?, '', 'completed', ?)",
            (task_id, rid, _now_iso()))
        # 扫雷⑨-H5：task-logs info 有任务归属校验，任务归属调用者 uid=1
        c.execute("UPDATE agent_tasks SET user_id='1' WHERE id=?", (task_id,))
        db.commit()
    return task_id


def _cleanup(cid: int, rid: int, task_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


class TestTaskLogsInfoSandboxUid8:

    def test_sandbox_engine_info_bits(self):
        """沙箱引擎：uid8 前 8 位 + 四段名 provider 派生 + 无 credential_id。"""
        from src.domains.runtime.routes.task_logs import get_task_runtime_info
        cid = _insert_credential("f81d4fae-9dec-11d0-a765-00a0c91e6bf6")
        rid = _insert_runtime(cid, "opencode-sandbox:hwcloud:codearts:inst1")
        task_id = _insert_task(rid)
        try:
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["credential_uid8"] == "f81d4fae", \
                "credential_uid8 应为 credential_uid 前 8 位（服务名单锚）"
            assert info["sandbox_provider"] == "codearts", \
                "四段名引擎应派生 provider=第 3 段（split(':')[2]，与 dashboard.py:1789 同口径；" \
                "opencode-sandbox:hwcloud:codearts:inst1 → codearts）"
            assert "credential_id" not in info, \
                "语义红线：credential_id 不下发（spec 拍板①，防 id 复活）"
        finally:
            _cleanup(cid, rid, task_id)

    def test_plain_engine_no_provider(self):
        """普通引擎（单段名）：sandbox_provider 为空串。"""
        from src.domains.runtime.routes.task_logs import get_task_runtime_info
        cid = _insert_credential("11111111-2222-3333-4444-555555555555")
        rid = _insert_runtime(cid, "opencode")
        task_id = _insert_task(rid)
        try:
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["sandbox_provider"] == "", "单段名引擎不应派生 provider"
            assert info["credential_uid8"] == "11111111"
            assert "credential_id" not in info
        finally:
            _cleanup(cid, rid, task_id)

    def test_empty_credential_uid_yields_empty_uid8(self):
        """credential_uid 空串（旧行缺值）：uid8 为空串不炸，不回退 id。"""
        from src.domains.runtime.routes.task_logs import get_task_runtime_info
        cid = _insert_credential("")
        rid = _insert_runtime(cid, "opencode-sandbox:hwcloud:codearts:inst2")
        task_id = _insert_task(rid)
        try:
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["credential_uid8"] == "", "空 credential_uid 应下发空 uid8"
            assert info["sandbox_provider"] == "codearts"
            assert "credential_id" not in info
        finally:
            _cleanup(cid, rid, task_id)
