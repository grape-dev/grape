# -*- coding: utf-8 -*-
"""Issue #3788：无场景绑定的 issue 事件被技能 scope 门禁 fail-closed 拦截。

报障（事件中心 1 小时 11 条 `skill_resolve_failed`，`scene=` 为空）：`feedback→issue` 与
API 建单不传 `scene_id` 时 issue 无场景 → 跳过场景节点技能覆盖 → session 级 user 档技能
进入统一入队口门禁 → 判官 `scene_skill_scope.evaluate` 的 `expected_scope = scene_scope
or "repo"` 把空场景按 repo 判定 → 必然 mismatch → 派发全断。

修复口径（实现侧）：
  1. 判官新增「无场景」**显式**入参 `scene_scope=None`：`ok=True`、`expected_scope=""`、
     `source="no_scene"`；`""` 与缺省仍回落 `repo`（四入口 #1722/#1758 语义零改动）。
  2. 派发侧门禁场景三态（`_gate_scene_context`）：`evt` 已带场景 → repo 口径；`evt` 缺失
     → 回读 Issue 真实绑定 + 仓库默认场景；**确认未绑定** → `scene_scope=None`；
     **未判定**（查询异常）→ 仍 repo 口径（fail-closed，不放行）。

测试 seam：判官 `evaluate()` + 派发三态 helper `_gate_scene_context()` + 统一入队口
`_enqueue_pipeline_task`（issue / PR 订阅 / A2A / fallback 全路径唯一入队收敛点）。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache  # noqa: E402


def _u():
    return uuid.uuid4().hex[:8]


def _repo_key():
    return f"gitcode:o3788-{_u()}/r-{_u()}"


def _mk_skill(scope="user", owner="42", sname="", agent_type="vod"):
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


def _seed_issue(repo_key, number, scene_id=""):
    """落 issue 卡片行（scene_id='' = 真实「未绑定场景」，复现本单触发条件）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "status, scene_id) VALUES (1, 'tester', ?, ?, 't', 'active', ?)",
            (repo_key, number, scene_id))
        db.commit()


def _seed_session_skill(repo_key, agent_type, sname):
    """session 级技能（user 档）——本单真实触发源：无场景覆盖时保留 session 技能。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_agent_session (repo_key, agent_type, skill, skill_id) "
            "VALUES (?,?,?,0)", (repo_key, agent_type, sname))
        db.commit()


def _create_scene(scene_id, repo_key, nodes):
    create_custom_scene(
        scene_id=scene_id, name=f"scene-{scene_id}", description="issue 3788 test",
        repo_key=repo_key, nodes=nodes)


def _cleanup(repo_key, scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
        db.commit()
    if scene_id:
        invalidate_scene_cache(scene_id, repo_key)


def _skill_resolve_failed_rows(repo_key):
    with db_lock:
        return db.cursor().execute(
            "SELECT seq, result, payload FROM events WHERE repo_key=? "
            "AND event_type='skill_resolve_failed'", (repo_key,)).fetchall()


class TestJudgeNoSceneScope:
    """R1：判官「无场景」显式入参（仅 None 触发，空串/缺省仍 repo）。"""

    def test_none_scene_scope_allows_user_skill(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk3788_{_u()}"
        try:
            _mk_skill("user", "42", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s},
                          repo, None, scene_id="")
            assert ev["ok"] is True
            assert ev["source"] == "no_scene"
            assert ev["expected_scope"] == ""
            assert ev["actual_scope"] == "user"
            assert "无场景" in ev["reason"]
        finally:
            _rm_skill(s)

    def test_empty_scene_scope_still_falls_back_to_repo(self):
        """空串不得被当成「无场景」——否则 #1722/#1758 的判定被静默关闭。"""
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk3788_{_u()}"
        try:
            _mk_skill("user", "42", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s}, repo, "")
            assert ev["ok"] is False
            assert ev["expected_scope"] == "repo"
            assert ev["source"] == "name"
        finally:
            _rm_skill(s)

    def test_default_scene_scope_unchanged(self):
        from src.api.scene_skill_scope import evaluate
        repo = _repo_key()
        s = f"usk3788_{_u()}"
        try:
            _mk_skill("user", "42", s)
            ev = evaluate({"id": "issue", "tag": "issue", "skill": s}, repo)
            assert ev["ok"] is False
            assert ev["expected_scope"] == "repo"
            assert ev["rebind_candidates"] == [] or isinstance(ev["rebind_candidates"], list)
        finally:
            _rm_skill(s)

    def test_no_scene_lookup_error_does_not_block_dispatch(self):
        """无场景不适用 repo 场景策略 → 技能查询异常也不得把派发打成 failed。"""
        from src.api.scene_skill_scope import evaluate
        import src.domains.mgmt.user.db as udb

        def _boom(*a, **kw):
            raise RuntimeError("db down")

        old = udb.get_skill_scope_info
        udb.get_skill_scope_info = _boom
        try:
            ev = evaluate({"skill": "any-skill"}, _repo_key(), None)
        finally:
            udb.get_skill_scope_info = old
        assert ev["ok"] is True
        assert ev["source"] == "no_scene"


class TestGateSceneContext:
    """R2：派发侧场景三态（`evt` → Issue 真实绑定 → 仓库默认场景）。"""

    def _ctx(self, **kw):
        from src.domains.runtime.pipeline.event_pipeline import _gate_scene_context
        return _gate_scene_context(**kw)

    def test_unbound_issue_is_confirmed_no_scene(self):
        repo = _repo_key()
        try:
            _seed_issue(repo, 1, "")
            assert self._ctx(repo_key=repo, evt={}, issue_id="#1") == ("", None)
        finally:
            _cleanup(repo)

    def test_evt_scene_wins_and_keeps_repo_scope(self):
        repo = _repo_key()
        try:
            assert self._ctx(repo_key=repo, evt={"scene_id": "sc-evt"},
                             issue_id="#1") == ("sc-evt", "repo")
            assert self._ctx(repo_key=repo, evt={"_scene_id": "sc-evt2"},
                             issue_id="#1") == ("sc-evt2", "repo")
        finally:
            _cleanup(repo)

    def test_bound_issue_keeps_repo_scope(self):
        repo = _repo_key()
        scene_id = f"sc-{_u()}"
        try:
            _create_scene(scene_id, repo, [
                {"id": "vod", "tag": "vod", "skill": "vod-cli", "enabled": True}])
            _seed_issue(repo, 1, scene_id)
            assert self._ctx(repo_key=repo, evt={}, issue_id="#1") == (scene_id, "repo")
        finally:
            _cleanup(repo, scene_id)

    def test_repo_dev_scene_fallback_keeps_repo_scope(self, monkeypatch):
        import src.domains.runtime.pipeline.event_pipeline as ep
        repo = _repo_key()
        try:
            _seed_issue(repo, 1, "")
            monkeypatch.setattr(ep, "_repo_dev_scene", lambda _rk: "sc-dev")
            assert self._ctx(repo_key=repo, evt={}, issue_id="#1") == ("sc-dev", "repo")
        finally:
            _cleanup(repo)

    def test_undetermined_scene_lookup_stays_repo_scope(self, monkeypatch):
        """查询异常（未判定）不得等同「确认无场景」——DB 抖动不放行。"""
        import src.domains.runtime.pipeline.event_pipeline as ep
        repo = _repo_key()
        try:
            _seed_issue(repo, 1, "")
            monkeypatch.setattr(ep, "_issue_authoritative_scene",
                                lambda *a, **k: None)
            assert self._ctx(repo_key=repo, evt={}, issue_id="#1") == ("", "repo")
        finally:
            _cleanup(repo)

    def test_undetermined_dev_scene_lookup_stays_repo_scope(self, monkeypatch):
        import src.domains.runtime.pipeline.event_pipeline as ep
        repo = _repo_key()
        try:
            _seed_issue(repo, 1, "")
            monkeypatch.setattr(ep, "_repo_dev_scene", lambda _rk: None)
            assert self._ctx(repo_key=repo, evt={}, issue_id="#1") == ("", "repo")
        finally:
            _cleanup(repo)


class TestDispatchGate:
    """R3：统一入队口门禁端到端（无场景放行 / 有场景仍拦截 user 档）。"""

    def _note_hook(self, owner, repo, number):
        return json.dumps({
            "project": {"path_with_namespace": f"{owner}/{repo}"},
            "object_attributes": {"noteable_type": "Issue", "noteable_iid": number,
                                  "note": "x", "id": 1},
            "user": {"username": "tester"},
        }).encode()

    def _run(self, repo_key, raw):
        import src.domains.runtime.pipeline.event_pipeline as ep
        ep._fail_skill_resolve_event  # noqa: B018 - 确保属性存在（test seam）
        blocked = []
        real = ep._fail_skill_resolve_event

        def _spy(*a, **kw):
            blocked.append(a[1] if len(a) > 1 else kw.get("skill_name"))
            return real(*a, **kw)

        ep._fail_skill_resolve_event = _spy
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        try:
            result = ep.run_pipeline(
                raw, {"X-GitCode-Event": "Note Hook"},
                account=repo_key.split(":", 1)[-1])
        finally:
            os.environ.pop("DEVOPS_EXECUTION_MODE", None)
            ep._fail_skill_resolve_event = real
        return result, blocked

    def test_no_scene_user_skill_not_blocked(self, caplog):
        """无场景 issue + session user 档技能 → 不落 skill_resolve_failed（本单主症状）。"""
        repo = _repo_key()
        owner, name = repo.split(":", 1)[1].split("/", 1)
        sname = f"usk3788_{_u()}"
        try:
            _mk_skill("user", "42", sname, agent_type="issue")
            _seed_issue(repo, 3788, "")
            _seed_session_skill(repo, "issue", sname)
            _seed_session_skill(repo, "vod", sname)
            with caplog.at_level("INFO"):
                result, blocked = self._run(repo, self._note_hook(owner, name, 3788))
            assert blocked == [], f"无场景不应触发 skill 门禁拦截: {blocked}"
            assert not result.get("skill_resolve_failed")
            assert _skill_resolve_failed_rows(repo) == []
            assert "decision=no_scene" in caplog.text
        finally:
            _rm_skill(sname)
            _cleanup(repo)

    def test_bound_repo_scene_still_blocks_user_skill(self):
        """有 repo 级场景 + user 档技能 → 仍 fail-fast（#1722/#1758 不退化）。"""
        repo = _repo_key()
        owner, name = repo.split(":", 1)[1].split("/", 1)
        scene_id = f"sc-{_u()}"
        sname = f"usk3788_{_u()}"
        try:
            _mk_skill("user", "42", sname, agent_type="issue")
            # 模拟「apply 校验漏过的存量脏场景」：节点绑定 user 档技能直接落库
            _create_scene(scene_id, repo, [
                {"id": "issue", "tag": "issue", "skill": sname, "enabled": True},
                {"id": "vod", "tag": "vod", "skill": sname, "enabled": True}])
            _seed_issue(repo, 3788, scene_id)
            _seed_session_skill(repo, "issue", sname)
            _seed_session_skill(repo, "vod", sname)
            result, blocked = self._run(repo, self._note_hook(owner, name, 3788))
            assert blocked, "有场景时 user 档技能必须被门禁拦截"
            assert len(blocked) == 1 and sname in str(blocked[0])
            rows = _skill_resolve_failed_rows(repo)
            assert rows, "应落 skill_resolve_failed 错误事件"
            assert "scope 不匹配" in (rows[0][1] or "")
        finally:
            _rm_skill(sname)
            _cleanup(repo, scene_id)