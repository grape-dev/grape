# -*- coding: utf-8 -*-
"""Issue #657：技能 scope 与场景（repo 级）scope 匹配校验。

回归背景（user feedback #284）：repo 级场景引用 user 级技能时，
apply 阶段无校验；派发阶段 resolve_skill_id 在 repo/official scope 找不到
→ skill_id=0（skill 解析为空）仍继续派发 → 任务 pending_self_report，
1800s 后 timeout_failed，全程无指向「scope 不匹配」的报错。

修复后双层校验：
  1. get_skill_scope_info：按 repo → official → user 顺序判定技能 scope 匹配；
  2. 场景 apply/create/update：节点技能仅 user scope 可用 → 400
     （文案含「scope 不匹配」+ 技能名 + scope 值）；
  3. 派发 fail-fast：解析失败 → 立即落 skill_resolve_failed 错误事件
     （status=failed，result 带 [skill_resolve_failed] 前缀 + scope 不匹配文案），
     不再派发（替代 1800s 超时）；
  4. 早期 smoke 场景回归：user 级技能 + repo 场景全链（apply 拒绝 +
     强行落库后派发 fail-fast）。
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


def _delete_skills(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


def _unique_repo():
    return f"r657-{uuid.uuid4().hex[:10]}"


def _create_scene(scene_id: str, repo_key: str, nodes: list):
    create_custom_scene(
        scene_id=scene_id, name=f"scene-{scene_id}", description="issue 657 test",
        repo_key=repo_key, nodes=nodes)


def _cleanup(repo_key: str, scene_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (scene_id, repo_key))
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()
    invalidate_scene_cache(scene_id, repo_key)


class TestGetSkillScopeInfo:
    """Issue #657：技能 scope 三态判定（repo → official → user）。"""

    def test_user_only_scope_mismatch(self):
        """仅 user scope 存在 → found=True, matched=False, scope=user。"""
        from src.user.db import get_skill_scope_info
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:o657/scopeinfo"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            info = get_skill_scope_info(sname, rk)
            assert info["found"] is True
            assert info["matched"] is False
            assert info["scope"] == "user"
            assert info["skill_id"] > 0
        finally:
            _delete_skills(sname)

    def test_repo_scope_matched(self):
        """repo scope（owner=repo_key）→ matched=True。"""
        from src.user.db import get_skill_scope_info
        sname = f"rsk657_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:o657/scopeinfo"
        try:
            _mk_skill_row(scope="repo", owner=rk, sname=sname)
            info = get_skill_scope_info(sname, rk)
            assert info["found"] is True and info["matched"] is True
            assert info["scope"] == "repo"
        finally:
            _delete_skills(sname)

    def test_official_scope_matched(self):
        """official scope → matched=True（repo 场景可引用）。"""
        from src.user.db import get_skill_scope_info
        sname = f"osk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="official", owner="", sname=sname)
            info = get_skill_scope_info(sname, "gitcode:o657/scopeinfo")
            assert info["found"] is True and info["matched"] is True
            assert info["scope"] == "official"
        finally:
            _delete_skills(sname)

    def test_user_plus_repo_same_name_prefers_repo(self):
        """user + repo 同名并存 → repo 可用 → matched=True（不误伤）。"""
        from src.user.db import get_skill_scope_info
        sname = f"both657_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:o657/scopeinfo"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            _mk_skill_row(scope="repo", owner=rk, sname=sname)
            info = get_skill_scope_info(sname, rk)
            assert info["matched"] is True
            assert info["scope"] == "repo"
        finally:
            _delete_skills(sname)

    def test_unknown_skill_not_found(self):
        """技能不在 skill 表（官方内置包形态）→ found=False（存量场景兼容）。"""
        from src.user.db import get_skill_scope_info
        info = get_skill_scope_info(f"nosuch657_{uuid.uuid4().hex[:6]}",
                                    "gitcode:o657/scopeinfo")
        assert info["found"] is False
        assert info["matched"] is False


class TestApplySceneScopeMismatch:
    """apply 拦截：repo 级场景引用 user 级技能 → 400（含技能名与 scope 值）。"""

    def test_apply_user_scope_skill_rejected(self):
        """user 级技能节点 → apply 直接 400，错误含「scope 不匹配」+ 技能名 + scope 值。"""
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            with pytest.raises(HTTPException) as ei:
                apply_scene(owner, repo, ApplySceneRequest(scene_id=scene_id), {})
            assert ei.value.status_code == 400
            msg = ei.value.detail["message"]
            assert "scope 不匹配" in msg
            assert sname in msg
            assert "scope=user" in msg and "scope=repo" in msg
        finally:
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)

    def test_apply_repo_scope_skill_passes(self):
        """repo 级技能（owner=repo_key）→ 不触发 scope 拦截。"""
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        sname = f"rsk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="repo", owner=repo_key, sname=sname)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": sname, "enabled": True}])
            result = apply_scene(owner, repo, ApplySceneRequest(scene_id=scene_id), {})
            assert "scope 不匹配" not in json.dumps(result, ensure_ascii=False)
        finally:
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)

    def test_apply_unknown_skill_keeps_legacy_behavior(self):
        """技能不在 skill 表（官方内置包形态）→ 维持原行为（兼容 Issue #524 存量用例）。"""
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        try:
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": "issue-cli-unknown-657",
                 "enabled": True}])
            result = apply_scene(owner, repo, ApplySceneRequest(scene_id=scene_id), {})
            assert "nodes" in result
        finally:
            _cleanup(repo_key, scene_id)


class TestCreateUpdateSceneScopeMismatch:
    """保存拦截：create/update 场景引用 user 级技能 → 400。"""

    def test_create_scene_user_scope_skill_rejected(self):
        from src.api.routes.scenes import create_scene, CreateSceneRequest, SceneNodeInput
        owner, repo = "o657", _unique_repo()
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            body = CreateSceneRequest(
                name="t", nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            with pytest.raises(HTTPException) as ei:
                create_scene(body, user={}, repo=f"{owner}/{repo}")
            assert ei.value.status_code == 400
            msg = ei.value.detail["message"]
            assert "scope 不匹配" in msg and sname in msg
            assert "scope=user" in msg and "scope=repo" in msg
        finally:
            _delete_skills(sname)

    def test_create_scene_own_user_scope_skill_rejected(self):
        """#1758 固定口径：本人 user 技能同样拒绝（派发身份是 agent，不是本人）。"""
        from src.api.routes.scenes import create_scene, CreateSceneRequest, SceneNodeInput
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        try:
            _mk_skill_row(scope="user", owner="1", sname=sname)
            body = CreateSceneRequest(
                name="t", scene_id=scene_id,
                nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            with pytest.raises(HTTPException) as ei:
                create_scene(body, user={"username": "me657", "id": 1},
                             repo=f"{owner}/{repo}")
            assert ei.value.status_code == 400
            detail = ei.value.detail
            assert "scope 不匹配" in detail["message"] and sname in detail["message"]
            assert "含创建者本人" in detail["message"]
            assert detail["skill_scope"] == "user"
            assert detail["scene_scope"] == "repo"
        finally:
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)

    def test_create_scene_other_user_scope_skill_rejected(self):
        """#657 follow-up：他人个人技能（owner!=当前用户）→ 仍 400。"""
        from src.api.routes.scenes import create_scene, CreateSceneRequest, SceneNodeInput
        owner, repo = "o657", _unique_repo()
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="user", owner="someoneelse", sname=sname)
            body = CreateSceneRequest(
                name="t", nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            with pytest.raises(HTTPException) as ei:
                create_scene(body, user={"username": "me657", "id": 1},
                             repo=f"{owner}/{repo}")
            assert ei.value.status_code == 400
            assert "user 级技能" in ei.value.detail["message"]
            assert "scope 不匹配" in ei.value.detail["message"]
        finally:
            _delete_skills(sname)

    def test_update_scene_user_scope_skill_rejected(self):
        from src.api.routes.scenes import (update_scene, UpdateSceneRequest,
                                           SceneNodeInput)
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            _create_scene(scene_id, repo_key, [
                {"id": "issue", "tag": "issue", "skill": "issue-cli", "enabled": True}])
            body = UpdateSceneRequest(
                nodes=[SceneNodeInput(id="issue", tag="issue", skill=sname)])
            with pytest.raises(HTTPException) as ei:
                update_scene(scene_id, body, user={}, repo=f"{owner}/{repo}")
            assert ei.value.status_code == 400
            msg = ei.value.detail["message"]
            assert "scope 不匹配" in msg and sname in msg
        finally:
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)


class TestDispatchFailFast:
    """派发兜底：apply 漏过的场景派发时 skill 解析为空 → 立即 skill_resolve_failed
    错误事件、不再派发（非 1800s pending_self_report 超时）。"""

    def _seed(self, repo_key, scene_id, sname, issue_number=657):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "status, scene_id) VALUES (1, 'tester', ?, ?, 't', 'active', ?)",
                (repo_key, issue_number, scene_id))
            db.commit()

    def test_run_pipeline_scene_skill_user_scope_fails_fast(self):
        """issue 绑定场景节点技能为 user 级 → 派发前 fail-fast。"""
        import src.api.event_pipeline as ep
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        sname = f"usk657_{uuid.uuid4().hex[:6]}"
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            # 直接落库（绕过 apply 校验，模拟 apply 漏过的存量场景）；
            # issue_comment 路由到 vod，场景须含 vod 节点才能走到技能覆盖解析
            _create_scene(scene_id, repo_key, [
                {"id": "vod", "tag": "vod", "skill": sname, "enabled": True}])
            self._seed(repo_key, scene_id, sname)
            raw = json.dumps({
                "project": {"path_with_namespace": f"{owner}/{repo}"},
                "object_attributes": {"noteable_type": "Issue", "noteable_iid": 657,
                                      "note": "x", "id": 1},
                "user": {"username": "tester"},
            }).encode()
            result = ep.run_pipeline(raw, {"X-GitCode-Event": "Note Hook"},
                                     account=f"{owner}/{repo}")
            # 1) 立即返回失败（不再派发）
            assert result.get("ok") is False or result.get("skill_resolve_failed") is True
            # 2) events 表落 skill_resolve_failed 错误事件（status=failed）
            with db_lock:
                c = db.cursor()
                rows = c.execute(
                    "SELECT status, result, agent_type FROM events "
                    "WHERE repo_key=? AND event_type='skill_resolve_failed'",
                    (repo_key,)).fetchall()
                tasks = c.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=?", (repo_key,)
                ).fetchone()[0]
            assert rows, "应产生 skill_resolve_failed 错误事件"
            status, result_text, agent_type = rows[0]
            assert status == "failed"
            assert "[skill_resolve_failed]" in result_text
            assert "scope 不匹配" in result_text
            assert sname in result_text
            assert "scope=user" in result_text
            # 3) 不再派发：无任务入队（pending_self_report 1800s 超时路径消失）
            assert tasks == 0, "skill 解析失败不应派发任务"
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)

    def test_fail_skill_resolve_event_helper_with_seq(self):
        """_fail_skill_resolve_event（seq>0）→ 既有事件置 failed + agent_tasks 同步。"""
        from src.api.event_store import append_event
        from src.api.event_pipeline import _fail_skill_resolve_event
        repo_key = f"gitcode:o657/{_unique_repo()}"
        seq = append_event(repo_key=repo_key, event_type="issue_comment",
                           title="t", issue_id="#1", agent_type="issue",
                           status="running")
        try:
            _fail_skill_resolve_event(
                repo_key, "usk657_x", "scene-x", "issue",
                "技能 'usk657_x' scope 不匹配（技能 scope=user，场景 scope=repo）",
                issue_id="#1", seq=seq)
            with db_lock:
                c = db.cursor()
                row = c.execute("SELECT status, result FROM events WHERE seq=?",
                                (seq,)).fetchone()
            assert row[0] == "failed"
            assert "[skill_resolve_failed]" in row[1]
            assert "scope 不匹配" in row[1]
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM events WHERE seq=?", (seq,))
                db.commit()


class TestSmokeRegression:
    """早期 smoke 场景回归：user 级技能 + repo 场景全链。"""

    def test_user_scope_skill_full_chain(self):
        """user 技能 → repo 场景：apply 拒绝 + 强行落库后派发 fail-fast。"""
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        import src.api.event_pipeline as ep
        owner, repo = "o657", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        scene_id = f"sc-{uuid.uuid4().hex[:8]}"
        sname = f"smk657_{uuid.uuid4().hex[:6]}"
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        try:
            _mk_skill_row(scope="user", owner="42", sname=sname)
            _create_scene(scene_id, repo_key, [
                {"id": "vod", "tag": "vod", "skill": sname, "enabled": True}])
            # apply 阶段拒绝
            with pytest.raises(HTTPException) as ei:
                apply_scene(owner, repo, ApplySceneRequest(scene_id=scene_id), {})
            assert ei.value.status_code == 400
            # 强行落库 issue 绑定（模拟存量脏数据）→ 派发 fail-fast
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, "
                    "title, status, scene_id) VALUES (1, 't', ?, 658, 't', 'active', ?)",
                    (repo_key, scene_id))
                db.commit()
            raw = json.dumps({
                "project": {"path_with_namespace": f"{owner}/{repo}"},
                "object_attributes": {"noteable_type": "Issue", "noteable_iid": 658,
                                      "note": "x", "id": 1},
                "user": {"username": "tester"},
            }).encode()
            ep.run_pipeline(raw, {"X-GitCode-Event": "Note Hook"},
                            account=f"{owner}/{repo}")
            with db_lock:
                c = db.cursor()
                row = c.execute(
                    "SELECT status, result FROM events WHERE repo_key=? "
                    "AND event_type='skill_resolve_failed'", (repo_key,)).fetchone()
                tasks = c.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=?", (repo_key,)
                ).fetchone()[0]
            assert row and row[0] == "failed"
            assert "scope 不匹配" in row[1] and sname in row[1]
            assert tasks == 0
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)
            _delete_skills(sname)
            _cleanup(repo_key, scene_id)
