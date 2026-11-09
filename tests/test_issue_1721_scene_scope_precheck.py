# -*- coding: utf-8 -*-
"""Issue #1721：scenes detail 节点级技能 scope 前置暴露 + audit 会话 token 只读通道。

覆盖：
  R1 detail 节点级 resolvable/scope_check（按技能名解析，不依赖 skill_id）；
  R2 无冲突/禁用/空技能节点行为不变；
  R3 GET /scenes/skill-scope-audit 支持 tk_ 只读本仓，显式越权仓 403；
  R4 audit 只读不写库，并输出 repo/official 级改绑建议。
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
from fastapi.testclient import TestClient  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402
from src.api.pipeline_config import (  # noqa: E402
    create_custom_scene, get_pipeline_config, invalidate_scene_cache)
from src.api.deps import get_current_user_or_api_key  # noqa: E402


def _mk_skill_row(scope="user", owner="", sname="", agent_type="issue"):
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


def _unique():
    return uuid.uuid4().hex[:10]


def _repo_key(owner, repo):
    return f"gitcode:{owner}/{repo}"


def _create_scene(scene_id, repo_key, nodes):
    create_custom_scene(
        scene_id=scene_id, name=f"scene-{scene_id}", description="issue 1721 test",
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


class TestSceneDetailScopeCheck:
    """R1/R2：detail 节点级增量字段。"""

    def test_detail_marks_user_scope_node_unresolvable(self):
        """报障形态：node skill_id=0，仅 user 级技能 → resolvable=false。"""
        from src.api.routes.scenes import get_scene_detail
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        s_repo = f"rsk1721_{_unique()}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=s_user)
            _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_user,
                 "skill_id": 0, "enabled": True},
                {"id": "coding", "tag": "coding", "skill": s_repo,
                 "skill_id": 0, "enabled": True},
            ])

            detail = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            by_tag = {n["tag"]: n for n in detail["nodes"]}

            issue = by_tag["issue"]
            assert issue["resolvable"] is False
            check = issue["scope_check"]
            assert check["ok"] is False
            assert check["skill"] == s_user
            assert check["actual_scope"] == "user"
            assert check["expected_scope"] == "repo"
            assert s_user in check["reason"]
            assert "scope 不匹配" in check["reason"]
            assert "user" in check["reason"] and "repo" in check["reason"]

            coding = by_tag["coding"]
            assert coding["resolvable"] is True
            assert coding["scope_check"]["ok"] is True
            assert coding["scope_check"]["actual_scope"] == "repo"

            # 缓存对象未被原地改写（detail 只复制附加）
            cached = get_pipeline_config(scene_id, repo_key=repo_key)
            for n in cached["nodes"]:
                assert "resolvable" not in n
                assert "scope_check" not in n
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)
            _delete_skill(s_repo)

    def test_detail_no_conflict_keeps_existing_fields(self):
        """无冲突场景：仅新增字段，既有节点字段保持不变。"""
        from src.api.routes.scenes import get_scene_detail
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_repo = f"rsk1721_{_unique()}"
        try:
            _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_repo,
                 "mode": "emit", "next": "done", "enabled": True},
            ])
            detail = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            node = detail["nodes"][0]
            assert node["id"] == "issue"
            assert node["tag"] == "issue"
            assert node["skill"] == s_repo
            assert node["mode"] == "emit"
            assert node["next"] == "done"
            assert node["resolvable"] is True
            assert node["scope_check"]["ok"] is True
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_repo)

    def test_detail_explicit_skill_id_is_authorized(self):
        """显式 skill_id 命中即授权：与派发侧 resolve_skill_dispatch 口径一致。"""
        from src.api.routes.scenes import get_scene_detail
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        try:
            user_skill_id = _mk_skill_row(scope="user", owner="42", sname=s_user)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_user,
                 "skill_id": user_skill_id, "enabled": True},
            ])
            detail = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            node = detail["nodes"][0]
            assert node["resolvable"] is True
            check = node["scope_check"]
            assert check["ok"] is True
            assert check["skill"] == s_user
            assert check["actual_scope"] == "user"
            assert "skill_id" in check["reason"]
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)

    def test_detail_invalid_skill_id_falls_back_to_name_gate(self):
        """skill_id 失效（技能被删/停用）→ 回退名字判定，仍报冲突。"""
        from src.api.routes.scenes import get_scene_detail
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=s_user)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_user,
                 "skill_id": 999999999, "enabled": True},
            ])
            detail = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            node = detail["nodes"][0]
            assert node["resolvable"] is False
            assert node["scope_check"]["actual_scope"] == "user"
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)

    def test_detail_after_rebind_to_repo_skill_is_resolvable(self):
        """AC4 回归：user 技能节点改绑 repo 级技能后详情可解析、派发门禁放行。"""
        from src.api.routes.scenes import get_scene_detail
        from src.api.event_pipeline import _skill_scope_mismatch
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        s_repo = f"rsk1721_{_unique()}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=s_user)
            repo_skill_id = _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_user,
                 "skill_id": 0, "enabled": True},
            ])
            before = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            assert before["nodes"][0]["resolvable"] is False

            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_repo,
                 "skill_id": repo_skill_id, "enabled": True},
            ])
            after = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            node = after["nodes"][0]
            assert node["resolvable"] is True
            assert node["scope_check"]["ok"] is True
            assert node["scope_check"]["actual_scope"] == "repo"
            mismatch, _info = _skill_scope_mismatch(s_repo, repo_key)
            assert mismatch is False
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)
            _delete_skill(s_repo)

    def test_detail_disabled_and_empty_skill_nodes_are_resolvable(self):
        """禁用节点与无技能节点不产生误报。"""
        from src.api.routes.scenes import get_scene_detail
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=s_user)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": s_user, "enabled": False},
                {"id": "done", "tag": "done", "skill": "", "enabled": True},
            ])
            detail = get_scene_detail(
                scene_id, repo=f"{owner}/{repo}", user={"username": "me", "id": 1})
            by_tag = {n["tag"]: n for n in detail["nodes"]}
            assert by_tag["issue"]["resolvable"] is True
            assert by_tag["issue"]["scope_check"]["ok"] is True
            assert "跳过" in by_tag["issue"]["scope_check"]["reason"]
            assert by_tag["done"]["resolvable"] is True
            assert by_tag["done"]["scope_check"]["ok"] is True
        finally:
            _cleanup_repo(repo_key, scene_id)
            _delete_skill(s_user)


class TestSkillScopeAuditSessionToken:
    """R3/R4：audit 会话 token 只读本仓 + 越权拒绝 + 改绑建议。"""

    def _setup_conflict_scene(self, agent_type="issue"):
        owner, repo = f"o1721-{_unique()}", _unique()
        repo_key = _repo_key(owner, repo)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1721_{_unique()}"
        s_repo = f"rsk1721_{_unique()}"
        _mk_skill_row(scope="user", owner="42", sname=s_user, agent_type=agent_type)
        _mk_skill_row(scope="repo", owner=repo_key, sname=s_repo, agent_type=agent_type)
        _create_scene(scene_id, repo_key, [
            {"id": "issue", "tag": "issue", "skill": s_user, "enabled": True},
        ])
        return owner, repo, repo_key, scene_id, s_user, s_repo

    def _teardown(self, repo_key, scene_id, *skills):
        _cleanup_repo(repo_key, scene_id)
        for s in skills:
            _delete_skill(s)

    def test_audit_session_token_reads_bound_repo(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        owner, repo, repo_key, scene_id, s_user, s_repo = self._setup_conflict_scene()
        try:
            result = audit_scene_skill_scope(
                repo="", limit=200,
                user={"auth_type": "session_token", "repo_key": repo_key})
            assert result["repo"] == repo_key
            assert result["read_only"] is True
            assert result["conflict_scenes"] == 1
            assert result["total_conflicts"] == 1
            v = result["violations"][0]
            assert v["skill"] == s_user
            assert v["actual_scope"] == "user"
            assert v["expected_scope"] == "repo"
            assert [s["skill_name"] for s in v["suggested_skills"]] == [s_repo]
            assert v["suggested_skills"][0]["scope"] == "repo"

            # 只读：nodes_json 未被改写
            with db_lock:
                after = db.cursor().execute(
                    "SELECT nodes_json FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=?", (scene_id, repo_key)).fetchone()[0]
            assert s_user in after
        finally:
            self._teardown(repo_key, scene_id, s_user, s_repo)

    def test_audit_session_token_explicit_same_repo_allowed(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        owner, repo, repo_key, scene_id, s_user, s_repo = self._setup_conflict_scene()
        try:
            result = audit_scene_skill_scope(
                repo=f"{owner}/{repo}", limit=200,
                user={"auth_type": "session_token", "repo_key": repo_key})
            assert result["repo"] == repo_key
            assert result["total_conflicts"] == 1
        finally:
            self._teardown(repo_key, scene_id, s_user, s_repo)

    def test_audit_session_token_explicit_other_repo_forbidden(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        owner, repo, repo_key, scene_id, s_user, s_repo = self._setup_conflict_scene()
        other_repo = f"gitcode:o1721-{_unique()}/{_unique()}"
        try:
            with pytest.raises(HTTPException) as ei:
                audit_scene_skill_scope(
                    repo=other_repo, limit=200,
                    user={"auth_type": "session_token", "repo_key": repo_key})
            assert ei.value.status_code == 403
        finally:
            self._teardown(repo_key, scene_id, s_user, s_repo)

    def test_audit_route_accepts_session_token(self):
        """路由级：tk_ 不再被 require_no_session_token 挡成 403。"""
        from src.api.main import app
        owner, repo = f"o1721-{_unique()}", _unique()
        user = {"id": 1, "username": "", "token": "tk_x",
                "token_type": "session_token", "auth_type": "session_token",
                "session_key": "s1721", "agent_type": "vod",
                "repo_key": _repo_key(owner, repo)}
        app.dependency_overrides[get_current_user_or_api_key] = lambda: user
        try:
            r = TestClient(app).get("/api/scenes/skill-scope-audit",
                                    headers={"Authorization": "Bearer tk_x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["repo"] == user["repo_key"]
        assert body["read_only"] is True

    def test_audit_session_token_without_repo_key_forbidden(self):
        from src.api.routes.scenes import audit_scene_skill_scope
        with pytest.raises(HTTPException) as ei:
            audit_scene_skill_scope(
                repo="", limit=200,
                user={"auth_type": "session_token", "repo_key": ""})
        assert ei.value.status_code == 403