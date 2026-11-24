# -*- coding: utf-8 -*-
"""Issue #3789：场景节点持久化 skill_id（repo→official）、派发以 id 权威、
缺 id 降级标记 ``skill_source=name_degraded``、存量回填迁移。

覆盖：
  R1 保存链路（create scene / market）解析并固化节点 skill_id；
  R2 同名 user 档技能存在时 repo 场景仍命中 repo/official（不误绑）；
  R3 派发降级来源标记 skill_source=name_degraded / skill_id；
  R4 存量迁移幂等（回填 + 纠正 #979 写入的 user 档），post_check 收敛；
  R5 应用链路按节点 skill_id 权威落 session，不落 user 档。
  R6（QA 复审）市场应用/复制写入口固化 skill_id；跨仓应用丢弃「模板属仓」repo 档
     id 并按目标 repo_key 重解析（不 400）；
  R7（QA 复审）悬空/失效 skill_id 清 0 后按名重解析；未启用节点在回填与 post_check
     中口径一致跳过。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.api.pipeline_config import invalidate_scene_cache  # noqa: E402


def _uid():
    return uuid.uuid4().hex[:10]


def _mk_skill(scope="repo", owner="", sname="", agent_type="issue"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (sname, sname, "desc", agent_type, sname, "", scope, owner))
        db.commit()
        return c.lastrowid


def _rm_skill(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


def _repo_key():
    return f"gitcode:o3789-{_uid()}/r-{_uid()}"


def _cleanup(repo_key, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (scene_id,))
        c.execute("DELETE FROM scene_market WHERE name=?", (scene_id,))
        db.commit()
    invalidate_scene_cache(scene_id, repo_key)


def _scene_nodes(scene_id):
    with db_lock:
        row = db.cursor().execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=?",
            (scene_id,)).fetchone()
    return json.loads(row[0]) if row else None


class TestSavePersistsSkillId:
    def test_create_scene_persists_repo_skill_id(self):
        from src.domains.mgmt.routes.scenes import (
            create_scene, CreateSceneRequest, SceneNodeInput)
        repo_key = _repo_key()
        owner, repo = repo_key.split(":", 1)[1].split("/")
        scene_id = f"sc3789_{_uid()}"
        sname = f"rsk3789_{_uid()}"
        try:
            sid = _mk_skill("repo", repo_key, sname)
            body = CreateSceneRequest(
                scene_id=scene_id, name=scene_id,
                nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            create_scene(body, user={"username": "me", "id": 1},
                         repo=f"{owner}/{repo}")
            nodes = _scene_nodes(scene_id)
            assert nodes and nodes[0].get("skill_id") == sid, (
                f"保存后节点应含 skill_id={sid}, got={nodes}")
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)

    def test_create_market_scene_persists_skill_id(self):
        from src.domains.mgmt.routes.scenes import (
            create_market_scene, CreateMarketSceneRequest)
        repo_key = _repo_key()
        owner, repo = repo_key.split(":", 1)[1].split("/")
        name = f"m3789_{_uid()}"
        sname = f"msk3789_{_uid()}"
        market_id = None
        try:
            sid = _mk_skill("repo", repo_key, sname)
            body = CreateMarketSceneRequest(
                name=name,
                nodes=[{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            resp = create_market_scene(body, user={"username": "me", "id": 1},
                                       repo=f"{owner}/{repo}")
            market_id = resp["id"]
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM scene_market WHERE id=?",
                    (market_id,)).fetchone()
            nodes = json.loads(row[0])
            assert nodes[0].get("skill_id") == sid
        finally:
            if market_id:
                with db_lock:
                    db.cursor().execute("DELETE FROM scene_market WHERE id=?", (market_id,))
                    db.commit()
            _rm_skill(sname)

    def test_saved_node_dispatch_has_no_missing_id_warning(self, caplog):
        """保存固化 skill_id 后派发不再出现 "node without skill_id" 告警。"""
        from src.domains.mgmt.routes.scenes import (
            create_scene, CreateSceneRequest, SceneNodeInput)
        from src.domains.mgmt.user.db import resolve_skill_dispatch
        repo_key = _repo_key()
        owner, repo = repo_key.split(":", 1)[1].split("/")
        scene_id = f"sc3789_{_uid()}"
        sname = f"wsk3789_{_uid()}"
        try:
            sid = _mk_skill("repo", repo_key, sname)
            body = CreateSceneRequest(
                scene_id=scene_id, name=scene_id,
                nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            create_scene(body, user={"username": "me", "id": 1},
                         repo=f"{owner}/{repo}")
            nodes = _scene_nodes(scene_id)
            with caplog.at_level("WARNING"):
                res = resolve_skill_dispatch(
                    sname, nodes[0].get("skill_id"), repo_key=repo_key,
                    scene_id=scene_id, node_tag="issue", agent_type="issue")
            assert res["skill_id"] == sid and res["degraded"] is False
            assert res["skill_source"] == "skill_id"
            assert not any("node without skill_id" in r.message for r in caplog.records)
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)


class TestDispatchRepoOverUser:
    def test_degrade_prefers_repo_and_marks_source(self, caplog):
        from src.domains.mgmt.user.db import resolve_skill_dispatch
        repo_key = _repo_key()
        sname = f"dsk3789_{_uid()}"
        try:
            sid_repo = _mk_skill("repo", repo_key, sname)
            _mk_skill("user", "999999", sname)
            with caplog.at_level("WARNING"):
                res = resolve_skill_dispatch(sname, 0, repo_key=repo_key,
                                             user_id="999999", scene_id="s3789",
                                             node_tag="issue")
            assert res["skill_id"] == sid_repo, "repo 档优先，不得回退 user 档"
            assert res["degraded"] is True
            assert res["skill_source"] == "name_degraded"
            assert any("name_degraded" in r.message for r in caplog.records)
        finally:
            _rm_skill(sname)

    def test_official_fallback_when_no_repo(self):
        from src.domains.mgmt.user.db import resolve_skill_dispatch
        repo_key = _repo_key()
        sname = f"dsk3789o_{_uid()}"
        try:
            sid_o = _mk_skill("official", "", sname)
            _mk_skill("user", "999999", sname)
            res = resolve_skill_dispatch(sname, 0, repo_key=repo_key,
                                         user_id="999999")
            assert res["skill_id"] == sid_o
            assert res["skill_source"] == "name_degraded"
        finally:
            _rm_skill(sname)


class TestBackfillMigration:
    def _insert_scene(self, scene_id, repo_key, nodes):
        with db_lock:
            db.cursor().execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, created_at, updated_at) "
                "VALUES (?,?,?,?,1,'now','now')",
                (scene_id, scene_id, repo_key, json.dumps(nodes)))
            db.commit()

    def test_backfill_repo_scope_and_idempotent(self):
        from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
            backfill_scene_nodes_skill_id)
        repo_key = _repo_key()
        scene_id = f"mig3789_{_uid()}"
        sname = f"msk3789_{_uid()}"
        sname2 = f"msk3789u_{_uid()}"
        try:
            sid_repo = _mk_skill("repo", repo_key, sname)
            sid_o = _mk_skill("official", "", sname)
            sid_user = _mk_skill("user", "999999", sname2)
            self._insert_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": sname, "enabled": True},
                # #979 误写 user 档：必须纠正
                {"id": "qa", "tag": "qa", "skill": sname2, "skill_id": sid_user,
                 "enabled": True},
            ])
            report = backfill_scene_nodes_skill_id()
            assert report["filled"] >= 1
            assert report["user_corrected"] >= 1
            nodes = _scene_nodes(scene_id)
            first = next(n for n in nodes if n["id"] == "issue")
            second = next(n for n in nodes if n["id"] == "qa")
            assert first.get("skill_id") == sid_repo
            assert first.get("skill_id") != sid_o
            # 无 repo/official 同名技能 → user 档被清空，不在 repo 场景保留
            assert not second.get("skill_id")
            # 幂等：再跑不改变
            report2 = backfill_scene_nodes_skill_id()
            nodes2 = _scene_nodes(scene_id)
            assert nodes2 == nodes
            assert report2["filled"] == 0 and report2["user_corrected"] == 0
        finally:
            _rm_skill(sname)
            _rm_skill(sname2)
            _cleanup(repo_key, scene_id)

    def test_user_market_scene_keeps_user_skill(self):
        from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
            backfill_scene_nodes_skill_id)
        name = f"um3789_{_uid()}"
        sname = f"umsk3789_{_uid()}"
        market_id = None
        try:
            sid_u = _mk_skill("user", "777", sname)
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT INTO scene_market (name, scope, source, source_user_id, "
                    "nodes_json, is_active, repo_key) VALUES (?, 'user', 'user', 777, ?, 1, '')",
                    (name, json.dumps([{"id": "issue", "tag": "issue",
                                        "skill": sname, "enabled": True}])))
                market_id = c.lastrowid
                db.commit()
            backfill_scene_nodes_skill_id()
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM scene_market WHERE id=?", (market_id,)).fetchone()
            nodes = json.loads(row[0])
            assert nodes[0].get("skill_id") == sid_u, "user 市场场景保留 user 档"
        finally:
            if market_id:
                with db_lock:
                    db.cursor().execute("DELETE FROM scene_market WHERE id=?", (market_id,))
                    db.commit()
            _rm_skill(sname)

    def test_post_check_rejects_user_skill_in_repo_scene(self):
        from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
            verify_repo_scenes_without_user_skill)
        from fastapi import HTTPException  # noqa: F401 - 保持导入兼容
        repo_key = _repo_key()
        scene_id = f"chk3789_{_uid()}"
        sname = f"chk3789_{_uid()}"
        try:
            sid_u = _mk_skill("user", "999999", sname)
            self._insert_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": sname,
                 "skill_id": sid_u, "enabled": True}])
            with pytest.raises(RuntimeError):
                verify_repo_scenes_without_user_skill()
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)


class TestApplyUsesSkillId:
    def test_apply_prefers_node_skill_id_over_same_name_user(self, monkeypatch):
        import src.api.pipeline_config as pc
        import src.domains.mgmt.routes.scenes as scenes_mod
        import src.api.session_store as session_store

        repo_key = _repo_key()
        scene_id = f"ap3789_{_uid()}"
        sname = f"apsk3789_{_uid()}"
        try:
            sid_repo = _mk_skill("repo", repo_key, sname)
            _mk_skill("user", "999999", sname)
            nodes = [{"id": "issue", "tag": "issue", "skill": sname,
                      "skill_id": sid_repo, "enabled": True}]
            monkeypatch.setattr(scenes_mod, "_resolve_repo_key", lambda r: repo_key)
            monkeypatch.setattr(pc, "get_pipeline_config", lambda sid, repo_key="": {
                "name": sid, "nodes": nodes})
            recorded = []
            monkeypatch.setattr(session_store, "upsert_agent_session",
                                lambda **kw: recorded.append(kw))
            from src.domains.mgmt.routes.scenes import apply_scene, ApplySceneRequest
            apply_scene("o3789", "r3789",
                        ApplySceneRequest(scene_id=scene_id),
                        user={"username": "tester", "id": 1})
            assert recorded, "apply 应 upsert agent session"
            assert recorded[0]["skill_id"] == sid_repo
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)


def _mk_market(name, repo_key, nodes, scope="repo"):
    from src.domains.mgmt.user.db import create_scene_market
    return create_scene_market(
        name=name, display_name=name, description="issue 3789 test",
        scope=scope, repo_key=repo_key, source_user_id=0,
        nodes_json=json.dumps(nodes, ensure_ascii=False))


def _del_market(market_id):
    with db_lock:
        db.cursor().execute("DELETE FROM scene_market WHERE id=?", (market_id,))
        db.commit()


def _cleanup_repo(repo_key):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        db.commit()


class TestMarketWriteEntriesPersist:
    """QA 复审 Required-2：市场应用/体验/复制三条写入口固化 skill_id。

    ``scene_market.nodes_json`` 会被官方同步按模板原文整列覆盖，模板也不含
    skill_id，因此「存量回填」不足以消除 name_degraded——必须在写入口按目标
    repo_key 重新解析并固化。
    """

    def test_apply_market_scene_rebases_foreign_repo_skill_id(self):
        """跨仓应用：模板带的「模板属仓」repo 档 id 不得 400，按目标仓重解析。"""
        from src.domains.mgmt.routes.scenes import apply_market_scene
        tpl_repo = _repo_key()          # 模板属仓
        tgt_repo = _repo_key()          # 应用目标仓
        tpl_owner, tpl_name = tpl_repo.split(":", 1)[1].split("/")
        tgt_owner, tgt_name = tgt_repo.split(":", 1)[1].split("/")
        sname = f"mkt3789_{_uid()}"
        market_id = None
        try:
            sid_tpl = _mk_skill("repo", tpl_repo, sname)
            sid_tgt = _mk_skill("repo", tgt_repo, sname)
            assert sid_tpl != sid_tgt
            market_id = _mk_market(
                f"mkt3789_{_uid()}", tpl_repo,
                [{"id": "issue", "tag": "issue", "skill": sname,
                  "skill_id": sid_tpl, "enabled": True}])
            apply_market_scene(market_id, user={"username": "me", "id": 1},
                               owner=tgt_owner, repo=tgt_name)
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene "
                    "WHERE market_scene_id=? AND repo_key=? LIMIT 1",
                    (market_id, tgt_repo)).fetchone()
            assert row, "应用市场模板应创建目标仓库场景"
            nodes = json.loads(row[0])
            assert nodes[0].get("skill_id") == sid_tgt, (
                f"跨仓应用应重解析为目标仓 skill_id={sid_tgt}, got={nodes[0]}")
        finally:
            _cleanup_repo(tgt_repo)
            _cleanup_repo(tpl_repo)
            if market_id:
                _del_market(market_id)
            _rm_skill(sname)

    def test_apply_market_scene_persists_id_when_template_has_none(self):
        """模板无 skill_id（官方同步覆盖后的常态）→ 应用落库即固化。"""
        from src.domains.mgmt.routes.scenes import apply_market_scene
        tgt_repo = _repo_key()
        tgt_owner, tgt_name = tgt_repo.split(":", 1)[1].split("/")
        sname = f"mkt3789n_{_uid()}"
        market_id = None
        try:
            sid = _mk_skill("repo", tgt_repo, sname)
            market_id = _mk_market(
                f"mkt3789n_{_uid()}", tgt_repo,
                [{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            apply_market_scene(market_id, user={"username": "me", "id": 1},
                               owner=tgt_owner, repo=tgt_name)
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene "
                    "WHERE market_scene_id=? AND repo_key=? LIMIT 1",
                    (market_id, tgt_repo)).fetchone()
            assert row
            nodes = json.loads(row[0])
            assert nodes[0].get("skill_id") == sid
        finally:
            _cleanup_repo(tgt_repo)
            if market_id:
                _del_market(market_id)
            _rm_skill(sname)

    def test_copy_scene_persists_skill_id(self, monkeypatch):
        """复制场景（源节点无 skill_id）→ 副本落库前即固化。

        注：本 base 分支 copy_scene 传入的 ``advanced_options`` 尚不被
        create_custom_scene 接受（既有集成缺陷，tests/mgmt/test_issue_1844_scene_copy.py
        已整体红），此处 stub 落库函数，只验证 skill_id 固化接线本身。
        """
        import src.api.deps as deps_mod
        import src.api.pipeline_config as pc
        monkeypatch.setattr(deps_mod, "check_repo_access", lambda *a, **k: None)
        repo_key = _repo_key()
        owner, repo = repo_key.split(":", 1)[1].split("/")
        src_scene_id = f"cps3789_{_uid()}"
        sname = f"cps3789_{_uid()}"
        captured = {}
        try:
            sid = _mk_skill("repo", repo_key, sname)
            pc.create_custom_scene(
                scene_id=src_scene_id, name=src_scene_id, description="issue 3789",
                repo_key=repo_key,
                nodes=[{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            monkeypatch.setattr(
                pc, "create_custom_scene",
                lambda **kw: (captured.update(kw), 4242)[1])
            from src.domains.mgmt.routes.scenes import copy_scene
            resp = copy_scene(src_scene_id, body=None,
                              user={"username": "me", "id": 1}, repo=f"{owner}/{repo}")
            assert resp["scene_id"]
            nodes = captured.get("nodes") or []
            assert nodes and nodes[0].get("skill_id") == sid, (
                f"副本节点应固化 skill_id={sid}, got={nodes}")
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, src_scene_id)

    def test_all_market_write_entries_wired(self):
        """写入口枚举守卫：新增写场景入口必须接 rebase + persist（防止再次漏接）。"""
        import inspect
        import src.domains.mgmt.routes.scenes as sm
        for fn in (sm.apply_market_scene, sm.experience_market_scene, sm.copy_scene,
                   sm.create_scene, sm.update_scene,
                   sm.create_market_scene, sm.update_market_scene):
            src = inspect.getsource(fn)
            assert "_persist_scene_skill_ids(" in src, f"{fn.__name__} 未接 _persist_scene_skill_ids"
        for fn in (sm.apply_market_scene, sm.experience_market_scene, sm.copy_scene):
            assert "_rebase_scene_skill_ids(" in inspect.getsource(fn), (
                f"{fn.__name__} 跨仓应用前未清不可移植 skill_id")


class TestDanglingSkillIdCleared:
    """QA 复审 Low：悬空 skill_id（技能已删/停用）必须清掉并按名重解析。"""

    def test_persist_replaces_dangling_id_by_name(self):
        from src.domains.mgmt.routes.scenes import _persist_scene_skill_ids
        repo_key = _repo_key()
        sname = f"dg3789_{_uid()}"
        try:
            sid = _mk_skill("repo", repo_key, sname)
            nodes = [{"id": "issue", "tag": "issue", "skill": sname,
                      "skill_id": 987654321, "enabled": True}]
            assert _persist_scene_skill_ids(nodes, repo_key, "s3789") == 1
            assert nodes[0]["skill_id"] == sid
        finally:
            _rm_skill(sname)

    def test_persist_clears_dangling_id_when_name_unresolved(self):
        from src.domains.mgmt.routes.scenes import _persist_scene_skill_ids
        repo_key = _repo_key()
        nodes = [{"id": "issue", "tag": "issue", "skill": f"nosuch_{_uid()}",
                  "skill_id": 987654322, "enabled": True}]
        _persist_scene_skill_ids(nodes, repo_key, "s3789")
        assert not nodes[0].get("skill_id"), "悬空 id 且名字不可解析时必须清 0"

    def test_backfill_clears_dangling_id(self):
        from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
            backfill_scene_nodes_skill_id)
        repo_key = _repo_key()
        scene_id = f"dgm3789_{_uid()}"
        sname = f"dgmsk3789_{_uid()}"
        try:
            sid = _mk_skill("repo", repo_key, sname)
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO devops_pipeline_scene "
                    "(scene_id, name, repo_key, nodes_json, is_active, created_at, updated_at) "
                    "VALUES (?,?,?,?,1,'now','now')",
                    (scene_id, scene_id, repo_key, json.dumps([
                        {"id": "issue", "tag": "issue", "skill": sname,
                         "skill_id": 987654323, "enabled": True}])))
                db.commit()
            report = backfill_scene_nodes_skill_id()
            assert report["dangling_cleared"] >= 1
            nodes = _scene_nodes(scene_id)
            assert nodes[0].get("skill_id") == sid
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)

    def test_backfill_and_post_check_skip_disabled_nodes(self):
        """口径统一：未启用节点不参与回填，也不算 post_check 违规。"""
        from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
            backfill_scene_nodes_skill_id, verify_repo_scenes_without_user_skill)
        repo_key = _repo_key()
        scene_id = f"dsp3789_{_uid()}"
        sname = f"dsps3789_{_uid()}"
        try:
            sid_user = _mk_skill("user", "999999", sname)
            raw = [{"id": "qa", "tag": "qa", "skill": sname,
                    "skill_id": sid_user, "enabled": False}]
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO devops_pipeline_scene "
                    "(scene_id, name, repo_key, nodes_json, is_active, created_at, updated_at) "
                    "VALUES (?,?,?,?,1,'now','now')",
                    (scene_id, scene_id, repo_key, json.dumps(raw)))
                db.commit()
            backfill_scene_nodes_skill_id()
            assert _scene_nodes(scene_id) == raw, "未启用节点不应被回填改写"
            verify_repo_scenes_without_user_skill()  # 不抛 = 未启用节点不计入违规
        finally:
            _rm_skill(sname)
            _cleanup(repo_key, scene_id)