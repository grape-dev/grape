"""Issue #979: 场景节点 skill_id 权威化（三档同名派发锁定）+ PR 订阅补 user_id。

验证点：
  1. 三档（official/repo/user）同名技能并存时，resolve_skill_dispatch 按
     skill_id 直查命中各自档位（id 权威）；id 失效/缺失按名字降级
     （user→repo→official）且 degraded=True + warning 日志；
  2. PR 订阅路径补 user_id：同名个人技能在传入 user_id 时解析到 user scope
     （不传 user_id 则落 official，证明 user_id 生效）；场景节点 skill_id
     可经 resolve_scene_node_skill_id 读取；
  3. 老场景（仅 skill_name）派发结果与既有 resolve_skill_id 名字解析一致
     （行为兼容）且日志出现降级 warning；
  4. migration backfill：仅含 skill_name 的存量场景节点写回 skill_id。
"""
import json
import uuid

import pytest

from src.user import db as udb
from src.session_manager import db, db_lock
from src.api import event_pipeline
from src.api.pipeline_config import create_custom_scene, get_node_by_tag
from src.api.db_setup import _fn_scene_nodes_skill_id_backfill


def _insert_scope_skill(sname, scope, owner_id, agent_type="issue"):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO skill (skill_name, agent_type, skill_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,1)",
            (sname, agent_type, f"/skills/{sname}", scope, str(owner_id)))
        db.commit()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1",
                  (sname, scope, str(owner_id)))
        row = c.fetchone()
    assert row, f"insert {scope} skill failed"
    return row[0]


def _delete_skills(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


def _mk_user(prefix="sk979"):
    from src.auth import grape_account_db
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    u = grape_account_db.create_grape_account(uname, email=None, password="pw123")
    if not u:
        u = grape_account_db.get_grape_account_by_username(uname)
    return u


def _cleanup_user(uname):
    from src.auth import grape_account_db
    u = grape_account_db.get_grape_account_by_username(uname)
    if u:
        grape_account_db.soft_delete_account(u["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


class TestTripleScopeIdLock:
    def test_id_authoritative_over_same_name(self, caplog):
        """三档同名并存：skill_id 直查各自命中，id 权威覆盖名字规则。"""
        ua = _mk_user()
        sname = f"clash979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_r = _insert_scope_skill(sname, "repo", rk)
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            for expect in (sid_o, sid_r, sid_u):
                res = udb.resolve_skill_dispatch(sname, expect, repo_key=rk, user_id=ua["id"],
                                                 scene_id="s979", node_tag="issue")
                assert res["skill_id"] == expect, (
                    f"skill_id={expect} 应权威命中, got {res['skill_id']}")
                assert res["skill_path"] == f"/skills/{sname}"
                assert res["degraded"] is False
        finally:
            _delete_skills(sname)
            _cleanup_user(ua["username"])

    def test_invalid_id_degrades_by_name_with_warning(self, caplog):
        """skill_id 失效 → 名字降级（user→repo→official）+ warning。"""
        ua = _mk_user()
        sname = f"clash979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            with caplog.at_level("WARNING"):
                res = udb.resolve_skill_dispatch(sname, 987654321, repo_key=rk,
                                                 user_id=ua["id"], scene_id="s979", node_tag="issue")
            # 降级走名字规则：user 优先（user_id 传入）→ 命中 user 档
            assert res["skill_id"] == sid_u
            assert res["degraded"] is True
            assert any("degrade to name resolution" in r.message for r in caplog.records)
            assert res["skill_id"] != sid_o
        finally:
            _delete_skills(sname)
            _cleanup_user(ua["username"])


class TestPrSubscriptionUserId:
    def test_user_id_resolves_user_scope(self):
        """PR 订阅口径：同名 official+user，传 user_id → user scope；不传 → official。"""
        ua = _mk_user()
        sname = f"prsub979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            # 补 user_id（Issue #979 修复前 PR 订阅只传 repo_key → 恒落 official）
            with_uid = udb.resolve_skill_dispatch(sname, 0, repo_key=rk, user_id=ua["id"])
            assert with_uid["skill_id"] == sid_u
            assert with_uid["skill"] and with_uid["skill"]["scope"] == "user"
            without_uid = udb.resolve_skill_dispatch(sname, 0, repo_key=rk)
            assert without_uid["skill_id"] == sid_o
        finally:
            _delete_skills(sname)
            _cleanup_user(ua["username"])

    def test_scene_node_skill_id_read_and_dispatch(self):
        """场景节点持久化 skill_id → resolve_scene_node_skill_id 可读，派发 id 命中。"""
        ua = _mk_user()
        sname = f"node979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        scene_id = f"sc979_{uuid.uuid4().hex[:8]}"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            nodes = [{"id": "n1", "tag": "qa", "skill": sname, "skill_id": sid_u,
                      "enabled": True}]
            db_id = create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                                        repo_key=rk, nodes=nodes)
            assert db_id
            node = get_node_by_tag(scene_id, "qa", repo_key=rk)
            assert node and node.get("skill_id") == sid_u
            got = event_pipeline.resolve_scene_node_skill_id(rk, "qa", scene_id=scene_id)
            assert got == sid_u
            # 派发：id 权威命中 user 档（即使不传 user_id 也不落 official）
            res = udb.resolve_skill_dispatch(sname, got, repo_key=rk)
            assert res["skill_id"] == sid_u and res["degraded"] is False
            assert res["skill_id"] != sid_o
        finally:
            _delete_skills(sname)
            with db_lock:
                db.cursor().execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (scene_id,))
                db.commit()
            _cleanup_user(ua["username"])


class TestLegacySceneCompat:
    def test_name_only_node_degrades_same_as_before(self, caplog):
        """老场景（仅 skill_name）：结果与 resolve_skill_id 名字解析一致 + warning。"""
        ua = _mk_user()
        sname = f"legacy979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        scene_id = f"sc979_{uuid.uuid4().hex[:8]}"
        try:
            sid_r = _insert_scope_skill(sname, "repo", rk)
            sid_o = _insert_scope_skill(sname, "official", "")
            nodes = [{"id": "n1", "tag": "issue", "skill": sname, "enabled": True}]
            db_id = create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                                        repo_key=rk, nodes=nodes)
            assert db_id
            # 节点无 skill_id → 0（老场景）
            assert event_pipeline.resolve_scene_node_skill_id(rk, "issue", scene_id=scene_id) == 0
            with caplog.at_level("WARNING"):
                res = udb.resolve_skill_dispatch(sname, 0, repo_key=rk, user_id=ua["id"],
                                                 scene_id=scene_id, node_tag="issue")
            legacy = udb.resolve_skill_id(sname, repo_key=rk, user_id=ua["id"])
            assert res["skill_id"] == legacy
            assert res["degraded"] is True
            assert any("node without skill_id" in r.message for r in caplog.records)
            assert res["skill_id"] in (sid_r, sid_o)
        finally:
            _delete_skills(sname)
            with db_lock:
                db.cursor().execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (scene_id,))
                db.commit()
            _cleanup_user(ua["username"])

    def test_migration_backfills_skill_id(self):
        """migration：仅名字的存量节点按 user→repo→official 写回 skill_id。"""
        ua = _mk_user()
        sname = f"mig979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979"
        scene_id = f"sc979_{uuid.uuid4().hex[:8]}"
        try:
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            sid_o = _insert_scope_skill(sname, "official", "")
            # 造存量行：created_by 映射到 ua（unified_user_mapping）
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO unified_user_mapping (user_id, gitcode_user) VALUES (?,?)",
                    (ua["id"], ua["username"]))
                db.commit()
            nodes = [{"id": "n1", "tag": "qa", "skill": sname, "enabled": True},
                     {"id": "n2", "tag": "sre", "skill": "", "enabled": True}]
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO devops_pipeline_scene "
                    "(scene_id, name, description, repo_key, nodes_json, is_active, created_by, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,1,?,?,?)",
                    (scene_id, scene_id, "", rk, json.dumps(nodes), ua["username"], "now", "now"))
                db.commit()
            _fn_scene_nodes_skill_id_backfill()
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=?",
                    (scene_id,)).fetchone()
            mig_nodes = json.loads(row[0])
            # user 上下文（created_by 映射）→ 命中 user 档
            assert mig_nodes[0].get("skill_id") == sid_u
            assert mig_nodes[0].get("skill_id") != sid_o
            # 幂等：再跑不变
            _fn_scene_nodes_skill_id_backfill()
            with db_lock:
                row2 = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=?",
                    (scene_id,)).fetchone()
            assert json.loads(row2[0])[0].get("skill_id") == sid_u
        finally:
            _delete_skills(sname)
            with db_lock:
                db.cursor().execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (scene_id,))
                db.cursor().execute("DELETE FROM unified_user_mapping WHERE user_id=?", (ua["id"],))
                db.commit()
            _cleanup_user(ua["username"])
