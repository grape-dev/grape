# -*- coding: utf-8 -*-
"""Issue #1758：场景节点技能 scope 判定收敛为单一判官。

覆盖：
  R1 判官 ``scene_skill_scope.evaluate`` 的口径（user 一律拒绝、显式 skill_id 仍需
     校验、repo/official 放行、失效 id 回退名字、查询异常 fail-closed、显式降级）；
  R2 四入口交叉断言：#1722 报障配置（repo 级场景 issue 节点按名引用 user 技能）
     在保存预检 / audit / 派发门禁 / scene_precheck / detail 得到同一结论；
  R3 400 结构化字段 skill_scope / scene_scope / rebind_candidates 可直接消费；
  R4 保存阶段一次性返回全部冲突节点并给出可执行改绑候选。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402
from src.api.pipeline_config import (  # noqa: E402
    create_custom_scene, invalidate_scene_cache)


def _unique():
    return uuid.uuid4().hex[:10]


def _mk_skill(scope="user", owner="", sname="", agent_type="issue"):
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
    return f"gitcode:o1758-{_unique()}/r-{_unique()}"


def _create_scene(scene_id, repo_key, nodes):
    create_custom_scene(
        scene_id=scene_id, name=f"scene-{scene_id}", description="issue 1758 test",
        repo_key=repo_key, nodes=nodes)


def _cleanup(repo_key, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (scene_id, repo_key))
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
        db.commit()
    invalidate_scene_cache(scene_id, repo_key)


class TestEvaluate:
    """R1：单一判官口径。"""

    def test_user_scope_rejected_even_for_owner(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk1758_{_unique()}"
        try:
            _mk_skill("user", "1", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s},
                          repo, "repo")
            assert ev["ok"] is False
            assert ev["actual_scope"] == "user"
            assert ev["expected_scope"] == "repo"
            assert "scope 不匹配" in ev["reason"]
            assert "含创建者本人" in ev["reason"]
            assert ev["source"] == "name"
        finally:
            _rm_skill(s)

    def test_explicit_user_skill_id_still_rejected(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk1758_{_unique()}"
        try:
            sid = _mk_skill("user", "42", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s,
                           "skill_id": sid}, repo, "repo")
            assert ev["ok"] is False
            assert ev["actual_scope"] == "user"
            assert ev["source"] == "skill_id"
        finally:
            _rm_skill(s)

    def test_explicit_repo_skill_id_allowed(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"rsk1758_{_unique()}"
        try:
            sid = _mk_skill("repo", repo, s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s,
                           "skill_id": sid}, repo, "repo")
            assert ev["ok"] is True
            assert ev["actual_scope"] == "repo"
            assert ev["source"] == "skill_id"
        finally:
            _rm_skill(s)

    def test_repo_skill_of_other_repo_rejected(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"rsk1758_{_unique()}"
        try:
            sid = _mk_skill("repo", f"gitcode:other/{_unique()}", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s,
                           "skill_id": sid}, repo, "repo")
            assert ev["ok"] is False
            assert ev["actual_scope"] == "repo"
            assert "归属仓库" in ev["reason"]
        finally:
            _rm_skill(s)

    def test_official_and_unknown_skills_allowed(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"osk1758_{_unique()}"
        try:
            _mk_skill("official", "", s)
            assert evaluate({"skill": s}, repo, "repo")["ok"] is True
            ev = evaluate({"skill": f"unknown-{_unique()}"}, repo, "repo")
            assert ev["ok"] is True
            assert ev["source"] == "unresolved"
        finally:
            _rm_skill(s)

    def test_invalid_skill_id_falls_back_to_name(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk1758_{_unique()}"
        try:
            _mk_skill("user", "42", s)
            ev = evaluate({"skill": s, "skill_id": 999999999}, repo, "repo")
            assert ev["ok"] is False
            assert ev["actual_scope"] == "user"
        finally:
            _rm_skill(s)

    def test_invalid_skill_id_without_name_rejected(self):
        from src.api.scene_skill_scope import evaluate
        ev = evaluate({"skill": "", "skill_id": 999999999}, _repo_key(), "repo")
        assert ev["ok"] is False
        assert ev["source"] == "invalid_skill_id"

    def test_disabled_and_empty_nodes_are_skipped(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk1758_{_unique()}"
        try:
            _mk_skill("user", "42", s)
            assert evaluate({"skill": s, "enabled": False}, repo, "repo")["ok"] is True
            assert evaluate({"skill": ""}, repo, "repo")["ok"] is True
        finally:
            _rm_skill(s)

    def test_lookup_error_fail_closed_by_default(self, monkeypatch):
        from src.api.scene_skill_scope import evaluate
        import src.user.db as udb

        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(udb, "get_skill_scope_info", _boom)
        ev = evaluate({"skill": "any-skill"}, _repo_key(), "repo")
        assert ev["ok"] is False
        assert ev["source"] == "db_error"
        assert "fail-closed" in ev["reason"]

    def test_lookup_error_explicit_degrade_allows(self, monkeypatch):
        from src.api.scene_skill_scope import evaluate
        import src.user.db as udb

        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(udb, "get_skill_scope_info", _boom)
        ev = evaluate({"skill": "any-skill"}, _repo_key(), "repo",
                      on_error="allow")
        assert ev["ok"] is True
        assert ev["source"] == "db_error"


class TestFourEntryConsistency:
    """R2：四入口（保存/audit/派发/precheck + detail）交叉断言。"""

    def _setup(self):
        repo = _repo_key()
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1758_{_unique()}"
        s_repo = f"rsk1758_{_unique()}"
        _mk_skill("user", "42", s_user)
        _mk_skill("repo", repo, s_repo)
        nodes = [{"id": "issue", "tag": "issue", "skill": s_user,
                  "skill_id": 0, "enabled": True}]
        _create_scene(scene_id, repo, nodes)
        return repo, scene_id, s_user, s_repo, nodes

    def test_report_scenario_same_conclusion_across_entries(self):
        from src.api.routes.scenes import (
            _validate_scene_skill_scope, audit_scene_skill_scope,
            get_scene_detail)
        from src.api.event_pipeline import _evaluate_skill_scope
        from src.api import scene_precheck

        repo, scene_id, s_user, s_repo, nodes = self._setup()
        try:
            # 1) 保存/apply 预检 → 400，含结构化字段与改绑候选
            with pytest.raises(HTTPException) as ei:
                _validate_scene_skill_scope(nodes, repo, "alice", "1")
            detail = ei.value.detail
            assert ei.value.status_code == 400
            assert detail["skill_scope"] == "user"
            assert detail["scene_scope"] == "repo"
            assert s_user in detail["message"]
            assert "scope 不匹配" in detail["message"]
            assert any(c["skill_name"] == s_repo
                       for c in detail["rebind_candidates"])

            # 2) audit 只读体检 → 同一冲突
            audit = audit_scene_skill_scope(repo=repo, limit=200, user={})
            assert audit["total_conflicts"] == 1
            av = audit["violations"][0]
            assert av["skill"] == s_user
            assert av["actual_scope"] == "user"
            assert av["expected_scope"] == "repo"
            assert any(c["skill_name"] == s_repo for c in av["suggested_skills"])

            # 3) 派发门禁 → ok=False，reason 与判官一致
            info = _evaluate_skill_scope(s_user, repo)
            assert info["ok"] is False
            assert info["actual_scope"] == "user"
            assert "scope 不匹配" in info["reason"]

            # 4) scene_precheck → skill_unresolvable
            pc = scene_precheck.precheck_scene(
                scene_id, repo, {"username": "alice", "id": "1"})
            assert pc["ok"] is False
            assert any(p["reason_code"] == "skill_unresolvable"
                       for p in pc["problems"])

            # 5) detail → resolvable=false，reason 同一口径
            scene_detail = get_scene_detail(
                scene_id, repo=repo.split(":", 1)[-1],
                user={"username": "alice", "id": 1})
            node = scene_detail["nodes"][0]
            assert node["resolvable"] is False
            assert node["scope_check"]["actual_scope"] == "user"
            assert "scope 不匹配" in node["scope_check"]["reason"]
        finally:
            _cleanup(repo, scene_id)
            _rm_skill(s_user)
            _rm_skill(s_repo)

    def test_save_returns_all_conflicting_nodes(self):
        from src.api.routes.scenes import _validate_scene_skill_scope
        repo = _repo_key()
        s1, s2 = f"usk1758_{_unique()}", f"usk1758_{_unique()}"
        try:
            _mk_skill("user", "42", s1)
            _mk_skill("user", "43", s2)
            nodes = [
                {"id": "issue", "tag": "issue", "skill": s1, "enabled": True},
                {"id": "coding", "tag": "coding", "skill": s2, "enabled": True},
            ]
            with pytest.raises(HTTPException) as ei:
                _validate_scene_skill_scope(nodes, repo, "alice", "1")
            detail = ei.value.detail
            assert len(detail["violations"]) == 2
            assert {v["skill"] for v in detail["violations"]} == {s1, s2}
            assert len(detail["rebinds"]) == 2
        finally:
            _rm_skill(s1)
            _rm_skill(s2)

    def test_rebind_candidates_follow_node_role_not_display_tag(self):
        """回归 QA 审查 AC2：tag 为展示名（非 agent_type）时仍能给出改绑候选。"""
        from src.api.routes.scenes import (
            _validate_scene_skill_scope, audit_scene_skill_scope)
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1758_{_unique()}"
        s_repo = f"rsk1758_{_unique()}"
        try:
            _mk_skill("user", "42", s_user, agent_type="coding")
            _mk_skill("repo", repo, s_repo, agent_type="coding")
            nodes = [{"id": "coding", "tag": "编码", "skill": s_user,
                      "skill_id": 0, "enabled": True}]
            _create_scene(scene_id, repo, nodes)

            ev = evaluate(nodes[0], repo, "repo")
            assert ev["ok"] is False
            assert [c["skill_name"] for c in ev["rebind_candidates"]] == [s_repo]

            with pytest.raises(HTTPException) as ei:
                _validate_scene_skill_scope(nodes, repo, "alice", "1")
            detail = ei.value.detail
            assert [c["skill_name"] for c in detail["rebind_candidates"]] == [s_repo]
            assert s_repo in detail["message"]

            audit = audit_scene_skill_scope(repo=repo, limit=200, user={})
            assert [c["skill_name"] for c in audit["violations"][0]["suggested_skills"]] == [s_repo]
        finally:
            _cleanup(repo, scene_id)
            _rm_skill(s_user)
            _rm_skill(s_repo)


class TestA2A400StructuredFields:
    """R3：a2a 派发失败 400 结构化字段。"""

    def test_fail_event_result_carries_scope_fields(self):
        from src.api.event_pipeline import _fail_skill_resolve_event
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk1758_{_unique()}"
        try:
            _mk_skill("user", "42", s)
            ev = evaluate({"skill": s, "tag": "issue", "id": "issue"},
                          repo, "repo")
            result = _fail_skill_resolve_event(
                repo, s, "scene-x", "issue", ev["reason"], scope_info=ev)
            assert result["skill_scope"] == "user"
            assert result["scene_scope"] == "repo"
            assert isinstance(result["rebind_candidates"], list)
            assert result["skill_resolve_failed"] is True
            with db_lock:
                row = db.cursor().execute(
                    "SELECT payload FROM events WHERE repo_key=? "
                    "AND event_type='skill_resolve_failed'",
                    (repo,)).fetchone()
            assert row
            payload = json.loads(row[0]) if row[0] else {}
            assert payload.get("skill_scope") == "user"
            assert payload.get("scene_scope") == "repo"
            assert "rebind_candidates" in payload
        finally:
            _cleanup(repo, "")
            _rm_skill(s)

    def test_a2a_endpoint_returns_structured_scope_fields(self, monkeypatch):
        """真实 /api/a2a 端点：issue 场景覆盖被 scope 判官拒绝 → 400 结构化字段。"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_user_or_none
        from src.api.routes import webhook as wh
        import src.api.session_store as store
        import src.api.event_pipeline as ep
        import src.user.db as user_db

        repo = _repo_key()
        owner, name = repo.split(":", 1)[1].split("/", 1)
        scene_id = f"sc-{_unique()}"
        s_user = f"usk1758_{_unique()}"
        _mk_skill("user", "42", s_user, agent_type="coding")
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT OR IGNORE INTO managed_repos "
                    "(repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                    (repo, owner, name, f"{owner}/{name}"))
                db.commit()

            app.dependency_overrides[get_user_or_none] = lambda: {
                "id": 1, "username": "alice", "auth_type": "jwt"}
            monkeypatch.setattr(user_db, "get_managed_repos",
                                lambda uid: [{"repo_key": repo}])
            monkeypatch.setattr(wh, "_issue_exists_in_db", lambda *a, **k: True)
            monkeypatch.setattr(wh, "_scene_node_check", lambda *a, **k: {
                "in_scene": True, "scene_id": scene_id,
                "nodes": [{"id": "coding"}]})
            monkeypatch.setattr(store, "get_issue_scene",
                                lambda *a, **k: (scene_id, 1))
            monkeypatch.setattr(store, "is_agent_enabled",
                                lambda *a, **k: True)
            monkeypatch.setattr(ep, "is_plugin_mode", lambda: True)
            monkeypatch.setattr(ep, "resolve_skill_from_pipeline",
                                lambda *a, **k: s_user)
            monkeypatch.setattr(ep, "resolve_scene_node_skill_id",
                                lambda *a, **k: 0)

            client = TestClient(app)
            resp = client.post("/api/a2a", json={
                "agent": "coding",
                "payload": {"repository": f"{owner}/{name}",
                            "issue_number": "5",
                            "context": "scope 400 endpoint test"},
            })
            assert resp.status_code == 400, resp.text
            data = resp.json()
            assert data["skill_resolve_failed"] is True
            assert data["skill_scope"] == "user"
            assert data["scene_scope"] == "repo"
            assert isinstance(data["rebind_candidates"], list)
        finally:
            app.dependency_overrides.pop(get_user_or_none, None)
            with db_lock:
                db.cursor().execute(
                    "DELETE FROM managed_repos WHERE repo_key=?", (repo,))
                db.commit()
            _cleanup(repo, scene_id)
            _rm_skill(s_user)
