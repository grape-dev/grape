# -*- coding: utf-8 -*-
"""Issue #1564：场景市场（模板）保存/应用路径的技能 scope 校验 + 存量体检。

回归背景（user feedback #2601）：A2A 派发应在流水线保存时校验技能作用域。
Issue #657 已覆盖 repo 场景 create/update/apply 的保存期校验，但市场模板
（scene_market）的保存与应用路径未校验，用户可通过模板应用绕过拦截，
冲突仍在派发期才暴露；同时 400 缺结构化 violations[]，前端无法按节点标红，
存量场景缺一次性只读体检。

本文件覆盖：
  R1 market create/update 强制拒绝 user scope（含创建者本人）；
  R2 market apply 以应用者身份复核（user scope 一律 400，含本人）；
  R3 400 一次性返回全部冲突节点 violations[]；
  R5 GET /scenes/skill-scope-audit 只读体检与汇总计数。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402
from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache  # noqa: E402


def _mk_skill_row(scope="user", owner="", sname="", agent_type="issue"):
    """直接插 skill 表行（绕开 create_skill 的目录副作用）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (sname, sname, "desc", agent_type, sname, "", scope, owner))
        db.commit()
        return c.lastrowid


def _delete_skill(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


def _unique_suffix():
    return uuid.uuid4().hex[:10]


def _unique_owner():
    return f"o1564-{_unique_suffix()}"


def _create_market(name, repo_key, nodes, scope="repo", source_user_id=0):
    from src.user.db import create_scene_market
    return create_scene_market(
        name=name, display_name=name, description="issue 1564 test",
        scope=scope, repo_key=repo_key, source_user_id=source_user_id,
        nodes_json=json.dumps(nodes, ensure_ascii=False))


def _delete_market(market_id):
    with db_lock:
        db.cursor().execute("DELETE FROM scene_market WHERE id=?", (market_id,))
        db.commit()


def _create_scene(scene_id, repo_key, nodes):
    create_custom_scene(
        scene_id=scene_id, name=f"scene-{scene_id}", description="issue 1564 test",
        repo_key=repo_key, nodes=nodes)


def _cleanup_repo(repo_key, scene_id=None):
    with db_lock:
        c = db.cursor()
        if scene_id:
            c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                      (scene_id, repo_key))
        else:
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        db.commit()
    if scene_id:
        invalidate_scene_cache(scene_id, repo_key)


class TestMarketSaveScope:
    """R1：市场模板 create/update 强制拒绝 user scope（含创建者本人）。"""

    def test_create_market_rejects_own_user_scope(self):
        """创建者本人引用的 user 技能同样被拒绝（模板全仓共享）。"""
        from src.api.routes.scenes import (create_market_scene,
                                           CreateMarketSceneRequest)
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"usk1564_{_unique_suffix()}"
        try:
            _mk_skill_row(scope="user", owner="1", sname=sname)
            body = CreateMarketSceneRequest(
                name=f"m-{_unique_suffix()}",
                nodes=[{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            with pytest.raises(HTTPException) as ei:
                create_market_scene(
                    body, user={"username": "me", "id": 1}, repo=f"{owner}/{repo}")
            assert ei.value.status_code == 400
            detail = ei.value.detail
            msg = detail["message"]
            assert "scope 不匹配" in msg and sname in msg
            assert "scope=user" in msg and "scope=repo" in msg
            assert detail["skill"] == sname
            assert detail["actual_scope"] == "user"
            assert detail["expected_scope"] == "repo"
            assert len(detail["violations"]) == 1
            assert detail["violations"][0]["node_id"] == "issue"
            assert detail["violations"][0]["node_tag"] == "issue"
        finally:
            _delete_skill(sname)

    def test_create_market_accepts_repo_scope(self):
        """repo 级技能不触发拦截，模板正常创建。"""
        from src.api.routes.scenes import (create_market_scene,
                                           CreateMarketSceneRequest)
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"rsk1564_{_unique_suffix()}"
        market_id = None
        try:
            _mk_skill_row(scope="repo", owner=repo_key, sname=sname)
            body = CreateMarketSceneRequest(
                name=f"m-{_unique_suffix()}",
                nodes=[{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            resp = create_market_scene(
                body, user={"username": "me", "id": 1}, repo=f"{owner}/{repo}")
            market_id = resp["id"]
            assert market_id > 0
        finally:
            if market_id:
                _delete_market(market_id)
            _delete_skill(sname)

    def test_update_market_rejects_user_scope_using_market_repo_key(self):
        """update 无 repo 入参，repo_key 取市场行自身归属，user scope 一律拒绝。"""
        from src.api.routes.scenes import (update_market_scene,
                                           UpdateMarketSceneRequest)
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"usk1564_{_unique_suffix()}"
        market_id = None
        try:
            _mk_skill_row(scope="user", owner="1", sname=sname)
            market_id = _create_market(
                f"m-{_unique_suffix()}", repo_key,
                [{"id": "issue", "tag": "issue", "skill": "issue-cli", "enabled": True}],
                source_user_id=1)
            body = UpdateMarketSceneRequest(
                nodes=[{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            with pytest.raises(HTTPException) as ei:
                update_market_scene(
                    market_id, body, user={"username": "me", "id": 1})
            assert ei.value.status_code == 400
            detail = ei.value.detail
            assert "scope 不匹配" in detail["message"] and sname in detail["message"]
            assert detail["violations"][0]["skill"] == sname
            assert detail["violations"][0]["expected_scope"] == "repo"
        finally:
            if market_id:
                _delete_market(market_id)
            _delete_skill(sname)

    def test_market_violations_return_all_conflicting_nodes(self):
        """R3：一次返回全部冲突节点，而非命中即抛。"""
        from src.api.routes.scenes import (create_market_scene,
                                           CreateMarketSceneRequest)
        owner, repo = _unique_owner(), _unique_suffix()
        s1 = f"usk1564_{_unique_suffix()}"
        s2 = f"usk1564_{_unique_suffix()}"
        market_id = None
        try:
            _mk_skill_row(scope="user", owner="42", sname=s1)
            _mk_skill_row(scope="user", owner="43", sname=s2)
            body = CreateMarketSceneRequest(
                name=f"m-{_unique_suffix()}",
                nodes=[
                    {"id": "issue", "tag": "issue", "skill": s1, "enabled": True},
                    {"id": "coding", "tag": "coding", "skill": s2, "enabled": True},
                ])
            with pytest.raises(HTTPException) as ei:
                create_market_scene(
                    body, user={"username": "me", "id": 1}, repo=f"{owner}/{repo}")
            detail = ei.value.detail
            assert len(detail["violations"]) == 2
            by_node = {v["node_id"]: v for v in detail["violations"]}
            assert set(by_node) == {"issue", "coding"}
            assert by_node["issue"]["skill"] == s1
            assert by_node["coding"]["skill"] == s2
            for v in detail["violations"]:
                assert set(v) == {"node_id", "node_tag", "skill",
                                  "actual_scope", "expected_scope"}
                assert v["actual_scope"] == "user"
                assert v["expected_scope"] == "repo"
            assert s1 in detail["message"] and s2 in detail["message"]
        finally:
            _delete_skill(s1)
            _delete_skill(s2)
            if market_id:
                _delete_market(market_id)


class TestMarketApplyScope:
    """R2：应用市场模板以应用者身份复核——user scope 一律 400（#1758 含本人）。"""

    def test_apply_market_rejects_other_user_skill(self):
        from src.api.routes.scenes import apply_market_scene
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"usk1564_{_unique_suffix()}"
        market_id = None
        try:
            _mk_skill_row(scope="user", owner="other", sname=sname)
            market_id = _create_market(
                f"m-{_unique_suffix()}", repo_key,
                [{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}],
                source_user_id=99)
            with pytest.raises(HTTPException) as ei:
                apply_market_scene(
                    market_id, user={"username": "me", "id": 1},
                    owner=owner, repo=repo)
            assert ei.value.status_code == 400
            detail = ei.value.detail
            assert "scope 不匹配" in detail["message"] and sname in detail["message"]
            assert detail["violations"][0]["node_id"] == "issue"
            assert detail["violations"][0]["actual_scope"] == "user"
        finally:
            _cleanup_repo(repo_key)
            if market_id:
                _delete_market(market_id)
            _delete_skill(sname)

    def test_apply_market_rejects_applicant_own_user_skill(self):
        """#1758 固定口径：应用者本人 user 技能同样拒绝（repo 场景全仓共享）。"""
        from src.api.routes.scenes import apply_market_scene
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"usk1564_{_unique_suffix()}"
        market_id = None
        try:
            _mk_skill_row(scope="user", owner="1", sname=sname)
            market_id = _create_market(
                f"m-{_unique_suffix()}", repo_key,
                [{"id": "issue", "tag": "issue", "skill": sname, "enabled": True}],
                source_user_id=1)
            with pytest.raises(HTTPException) as ei:
                apply_market_scene(
                    market_id, user={"username": "me", "id": 1},
                    owner=owner, repo=repo)
            assert ei.value.status_code == 400
            detail = ei.value.detail
            assert "scope 不匹配" in detail["message"] and sname in detail["message"]
            assert detail["skill_scope"] == "user"
            assert detail["scene_scope"] == "repo"
        finally:
            _cleanup_repo(repo_key)
            if market_id:
                _delete_market(market_id)
            _delete_skill(sname)


class TestSkillScopeAudit:
    """R5：存量场景只读体检接口。"""

    def test_audit_lists_conflicts_and_is_read_only(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{_unique_suffix()}"
        s_user = f"usk1564_{_unique_suffix()}"
        s_repo = f"rsk1564_{_unique_suffix()}"
        nodes = [
            {"id": "issue", "tag": "issue", "skill": s_user, "enabled": True},
            {"id": "coding", "tag": "coding", "skill": s_repo, "enabled": True},
        ]
        try:
            _mk_skill_row(scope="user", owner="42", sname=s_user)
            _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo)
            _create_scene(scene_id, repo_key, nodes)
            with db_lock:
                before = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=?", (scene_id, repo_key)).fetchone()[0]

            result = audit_scene_skill_scope(
                repo=f"{owner}/{repo}", limit=200, user={})

            assert result["read_only"] is True
            assert result["checked_scenes"] == 1
            assert result["conflict_scenes"] == 1
            assert result["total_conflicts"] == 1
            v = result["violations"][0]
            assert v["scene_id"] == scene_id
            assert v["node_id"] == "issue"
            assert v["node_tag"] == "issue"
            assert v["skill"] == s_user
            assert v["actual_scope"] == "user"
            assert v["expected_scope"] == "repo"
            assert result["note"]

            # 只读：nodes_json 未被改写
            with db_lock:
                after = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=?", (scene_id, repo_key)).fetchone()[0]
            assert before == after
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)
            _delete_skill(s_repo)

    def test_audit_clean_repo_returns_zero_conflicts(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        owner, repo = _unique_owner(), _unique_suffix()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{_unique_suffix()}"
        s_repo = f"rsk1564_{_unique_suffix()}"
        try:
            _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_repo, "enabled": True}])
            result = audit_scene_skill_scope(
                repo=f"{owner}/{repo}", limit=200, user={})
            assert result["checked_scenes"] == 1
            assert result["total_conflicts"] == 0
            assert result["violations"] == []
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_repo)

    def test_audit_requires_repo(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        with pytest.raises(HTTPException) as ei:
            audit_scene_skill_scope(repo="", limit=200, user={})
        assert ei.value.status_code == 400


class TestAuditRouteAndFallback:
    """R5 路由顺序守卫 + audit 兜底仅针对缺列（QA 判断项）。"""

    def test_audit_route_registered_before_scene_id_route(self):
        """只读体检为固定路径，必须注册在 /{scene_id} 之前，否则被路径吞掉。"""
        import inspect
        from src.api.routes import scenes
        src = inspect.getsource(scenes)
        i_audit = src.index('@router.get("/skill-scope-audit"')
        i_detail = src.index('@router.get("/{scene_id}"')
        assert i_audit < i_detail, "skill-scope-audit 路由应在 /{scene_id} 之前注册"

    def test_audit_falls_back_only_on_missing_display_name_column(self, monkeypatch):
        """缺 display_name 列时定向兜底，正常返回体检结果。"""
        import sqlite3
        import src.session_manager as sm

        class _FakeCursor:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params=None):
                if "display_name" in sql:
                    raise sqlite3.OperationalError("no such column: display_name")
                self._rows = [("sc-old", "old-name", "[]")]

            def fetchall(self):
                return self._rows

        class _FakeDB:
            def cursor(self):
                return _FakeCursor()

        monkeypatch.setattr(sm, "db", _FakeDB())
        from src.api.routes.scenes import audit_scene_skill_scope
        result = audit_scene_skill_scope(repo="gitcode:o/r", limit=200, user={})
        assert result["checked_scenes"] == 1
        assert result["total_conflicts"] == 0

    def test_audit_propagates_unrelated_db_error(self, monkeypatch):
        """非缺列 DB 异常不得被吞成空/部分结果，应照常上抛。"""
        import src.session_manager as sm

        class _FakeCursor:
            def execute(self, sql, params=None):
                raise RuntimeError("disk I/O error")

            def fetchall(self):
                return []

        class _FakeDB:
            def cursor(self):
                return _FakeCursor()

        monkeypatch.setattr(sm, "db", _FakeDB())
        from src.api.routes.scenes import audit_scene_skill_scope
        with pytest.raises(RuntimeError):
            audit_scene_skill_scope(repo="gitcode:o/r", limit=200, user={})