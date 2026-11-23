"""sre-repo-worker-relax 约束回归 → Issue #3396 拆除后的新语义测试。

Issue #3396 删除 SRE 禁广播特例（_sre_no_broadcast 及写侧/状态侧配套特判）：
- SRE 与其他角色同走通用选择链：未绑定节点 = 广播模式（""），不再全灭（A1）。
- 未勾 sre 也未勾 all 的引擎派发/认领两侧不可调度（A2，role_no_candidates 留痕）。
- 场景写侧：SRE 空列表可保存、global 引擎可绑定（角色能力标签校验保留）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("GRAPE_DB_BACKEND", "sqlite")

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import engine_select as es


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id="", owner_name="tester"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, cap_tags=None, scope="global", owner_id=""):
    # Issue #3396（新基线）：远端凭据引擎需带可信执行器探活（probe_status=ok +
    # worker_version）才能过 claim_by_tag 的 9491 幽灵引擎闸门。
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id, probe_status, worker_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, "online", 1, 1, _now(), scope, owner_id, "ok", "1.0.0"))
        rid = c.lastrowid
        tags = ["all"] if cap_tags is None else list(cap_tags)
        for t in tags:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)", (rid, t))
        db.commit()
        return rid


class TestSreNoBroadcastRemoved(unittest.TestCase):
    """A1：SRE 禁广播拆除——未绑定节点 = 广播模式；global 池 sre/all 引擎可入池。"""

    def test_sre_no_broadcast_helper_deleted(self):
        self.assertFalse(hasattr(es, "_sre_no_broadcast"),
                         "Issue #3396：_sre_no_broadcast 应已删除")

    def test_unbound_sre_scene_broadcasts(self):
        """SRE 节点未绑定引擎 → 返回 ""（全量广播），不再 None（禁广播全灭）。"""
        from src.domains.mgmt.pipeline_config import create_custom_scene
        try:
            create_custom_scene(scene_id="sre-relax-broadcast", name="sre-relax-broadcast",
                                 description="", repo_key="owner/repo",
                                 nodes=[{"id": "sre", "tag": "sre", "skill": "test-skill",
                                         "mode": "emit", "next": "done", "handoff": "complete",
                                         "engine_runtime_ids": []}])
            self.assertEqual(
                es.get_scene_pool_engine_ids("sre-relax-broadcast", "sre",
                                             repo_key="owner/repo", user_id=1), "")
        finally:
            with db_lock:
                db.cursor().execute(
                    "DELETE FROM devops_pipeline_scene WHERE scene_id='sre-relax-broadcast' OR name='sre-relax-broadcast'")
                db.commit()

    def test_sre_task_claimed_by_sre_tagged_global_engine(self):
        """A1 认领侧：勾了 sre 的 global 引擎可认领 SRE 池任务（旧约束 global 不放行）。"""
        from src.domains.runtime.engine import service
        rid = _mk_engine("opencode", credential_id=_mk_cred("sre-relax-g1"), cap_tags=["sre"])
        task = service.enqueue_task(
            agent_session_id="sess-sre-relax", runtime_id=0, prompt="p",
            event_seq=339601, issue_id="#3396", repo_key="gitcode:sre-relax/repo",
            agent_type="sre", skill="sre", pool_agent_type="sre", pool_tag="sre")
        try:
            got = service.claim_by_tag("sre", rid)
            self.assertIsNotNone(got)
            self.assertEqual(got["id"], task)
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM agent_tasks WHERE id=?", (task,))
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                c.execute("DELETE FROM runtime_credentials WHERE name='sre-relax-g1'")
                db.commit()

    def test_sre_engine_without_cap_cannot_claim(self):
        """A2：未勾 sre 也未勾 all 的引擎不可认领 SRE 任务。"""
        from src.domains.runtime.engine import service
        rid = _mk_engine("opencode", credential_id=_mk_cred("sre-relax-g2"), cap_tags=["coding"])
        task = service.enqueue_task(
            agent_session_id="sess-sre-relax2", runtime_id=0, prompt="p",
            event_seq=339602, issue_id="#3396", repo_key="gitcode:sre-relax/repo",
            agent_type="sre", skill="sre", pool_agent_type="sre", pool_tag="sre")
        try:
            self.assertIsNone(service.claim_by_tag("all", rid))
            self.assertIsNone(service.claim_by_tag("sre", rid))
            self.assertEqual(service.get_task(task)["status"], "queued")
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM agent_tasks WHERE id=?", (task,))
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
                c.execute("DELETE FROM runtime_credentials WHERE name='sre-relax-g2'")
                db.commit()


class TestValidateSceneEngineIds(unittest.TestCase):
    """写侧校验：SRE 特例删除（空列表可存、global 引擎可绑），角色能力校验保留。"""

    def _validate(self, nodes, username="alice", repo_key="gitcode:org/repo"):
        from fastapi import HTTPException
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        try:
            _validate_scene_engine_ids(nodes, repo_key, username)
            return None
        except HTTPException as e:
            if isinstance(e.detail, dict):
                return e.detail.get("message", str(e.detail))
            return str(e.detail)

    def test_sre_empty_list_allowed(self):
        """Issue #3396：SRE 空列表 = 广播模式，允许保存（旧口径 400 拒绝）。"""
        self.assertIsNone(self._validate([{"tag": "sre", "enabled": True, "engine_runtime_ids": []}]))

    def test_sre_disabled_empty_ok(self):
        self.assertIsNone(self._validate([{"tag": "sre", "enabled": False, "engine_runtime_ids": []}]))


if __name__ == "__main__":
    unittest.main()
