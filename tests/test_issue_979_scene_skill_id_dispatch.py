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


# ---------------------------------------------------------------------------
# QA 复审补充：管线级集成测试（PR-sub 分支与 issue 场景覆盖分支，event_pipeline
# run_pipeline 主路由端到端）+ backfill scene_market repo 档 + get_node_by_tag
# user_id（个人场景节点查找）
# ---------------------------------------------------------------------------
from unittest.mock import patch


_TEST_REPO = "gitcode:owner979/repo"


def _integration_clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:owner979%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE '%979-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE '979-%'")
        db.commit()


def _seed_managed_repo():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "webhook_id, dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username) "
            "VALUES (?, 'owner979', 'repo', 'owner979/repo', 0, 'default', 'tk-979', 'wt-979', 'grape_bot')",
            (_TEST_REPO,))
        db.commit()


def _seed_pr_sub_scene(scene_id, qa_node, work_branch="mys-local"):
    nodes = [qa_node]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, pr_subscription_enabled, advanced_options) "
            "VALUES (?,?,?,?,1,1,?)",
            (scene_id, scene_id, _TEST_REPO, json.dumps(nodes),
             json.dumps({"work_branch": work_branch})))
        db.commit()


def _seed_qa_session():
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?, 'global', 'tester', 'active')",
            ("h979" + uuid.uuid4().hex, "979-cred", "979-dev"))
        _cid = c.lastrowid
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                  "VALUES ('opencode-979','opencode-979',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
        _rid = c.lastrowid
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable, repo_gitcode_token) "
                  "VALUES (?, ?, ?, ?, 'idle', 1, 'tk-979')",
                  ("sess-979-qa", _TEST_REPO, "qa", _rid))
        db.commit()


def _make_pr_opened_raw(pr_num=331, target_branch="mys-local"):
    return json.dumps({
        "object_attributes": {
            "action": "open",
            "target_branch": target_branch,
            "iid": pr_num,
            "title": "pr for 979",
        },
        "project": {"path_with_namespace": "owner979/repo"},
        "user": {"username": "human_user"},
    }).encode()


def _make_issue_note_raw(issue_num):
    return json.dumps({
        "project": {"path_with_namespace": "owner979/repo"},
        "object_attributes": {"noteable_type": "Issue", "noteable_iid": issue_num,
                              "note": "please review", "id": 1},
        "user": {"username": "tester"},
    }).encode()


def _run_hook(raw, headers, account="owner979/repo"):
    """走 run_pipeline 主路由，捕获 enqueue_task 全量 kwargs（skill_id 断言用）。"""
    import src.api.event_pipeline as ep
    from contextlib import ExitStack
    captured = []

    def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return "t_979_" + uuid.uuid4().hex[:8]

    _patches = [
        patch("src.runtime.engine_select.pick_engine", return_value=0),
        patch("src.runtime.engine_select.engine_name", return_value="opencode"),
        patch("src.runtime.engine_select.get_scene_pool_engine_ids", return_value=""),
        patch("src.api.event_pipeline.is_plugin_mode", return_value=True),
        patch("src.runtime.service.get_execution_mode", return_value="remote"),
        patch("src.runtime.service.enqueue_task", side_effect=_fake_enqueue),
        patch("src.runtime.service.verify_skill_materializable",
              return_value=(True, "test-stub", 0)),
        patch("src.user.db.get_agent_bot_token", return_value=""),
        patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]),
    ]
    with ExitStack() as stack:
        for p in _patches:
            stack.enter_context(p)
        ep.run_pipeline(raw, headers, account=account)
    return captured


class TestPRSubPipelineIntegration:
    """PR 订阅派发分支（run_pipeline _is_pr_sub）端到端：节点 skill_id 权威 +
    user_id 补传；降级（无 id）时 #657 门禁仍生效。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _integration_clean()
        _seed_managed_repo()
        _seed_qa_session()
        yield
        _integration_clean()

    def test_pr_sub_node_skill_id_authoritative_dispatch(self):
        """节点带 skill_id（repo 档）→ 入队 skill_id/skill_path 命中 repo 档。

        Issue #1758：显式 skill_id 仍需 scope 校验，故这里用 repo 档验证
        “id 权威覆盖同名 official”而不是 user 档。
        """
        sname = f"pip979_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc979p_{uuid.uuid4().hex[:8]}"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_repo = _insert_scope_skill(sname, "repo", _TEST_REPO)
            _seed_pr_sub_scene(scene_id, {"id": "qa", "tag": "qa", "skill": sname,
                                          "skill_id": sid_repo, "enabled": True})
            with patch("src.runtime.engine_select._repo_user_id", return_value=0):
                captured = _run_hook(_make_pr_opened_raw(pr_num=331),
                                     {"X-GitCode-Event": "Merge Request Hook"})
            assert captured, "PR 订阅应派发 qa 任务"
            for kw in captured:
                assert kw.get("skill_id") == sid_repo, (
                    f"PR-sub 节点 skill_id 应权威命中 repo 档 {sid_repo}, got {kw.get('skill_id')}")
                assert kw.get("skill_id") != sid_o
                assert kw.get("skill") == sname
                assert kw.get("skill_path") == f"/skills/{sname}"
            with db_lock:
                failed = db.cursor().execute(
                    "SELECT COUNT(*) FROM events WHERE repo_key=? "
                    "AND event_type='skill_resolve_failed'", (_TEST_REPO,)).fetchone()[0]
            assert failed == 0
        finally:
            _delete_skills(sname)

    def test_pr_sub_legacy_node_user_scope_only_fail_fast(self):
        """老场景（无 skill_id）+ 技能仅 user scope（他人）→ 降级解析为空 →
        #657 名字级门禁 fail-fast，不入队。"""
        sname = f"pip979f_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc979q_{uuid.uuid4().hex[:8]}"
        try:
            _insert_scope_skill(sname, "user", "999999")
            _seed_pr_sub_scene(scene_id, {"id": "qa", "tag": "qa", "skill": sname,
                                          "enabled": True})
            with patch("src.runtime.engine_select._repo_user_id", return_value=0):
                captured = _run_hook(_make_pr_opened_raw(pr_num=332),
                                     {"X-GitCode-Event": "Merge Request Hook"})
            assert not captured, "降级解析为空 + scope 不匹配不应入队"
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT COUNT(*) FROM events WHERE repo_key=? "
                    "AND event_type='skill_resolve_failed'", (_TEST_REPO,)).fetchone()[0]
            assert rows >= 1, "应落 skill_resolve_failed 错误事件"
        finally:
            _delete_skills(sname)


class TestIssueSceneOverrideIntegration:
    """issue 场景覆盖分支（run_pipeline plugin override）端到端：节点 skill_id
    权威（repo 档可覆盖同名 official）；显式 user 档仍需 scope 校验（#1758）。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _integration_clean()
        _seed_managed_repo()
        yield
        _integration_clean()

    def _seed_issue_with_scene(self, scene_id, scene_uid):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "status, scene_id, scene_user_id) "
                "VALUES (1, 'tester', ?, 700, 't', 'active', ?, ?)",
                (_TEST_REPO, scene_id, scene_uid))
            db.commit()

    def _seed_scene(self, scene_id, nodes):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active) VALUES (?,?,?,?,1)",
                (scene_id, scene_id, _TEST_REPO, json.dumps(nodes)))
            db.commit()

    def test_issue_scene_node_skill_id_overrides_official(self):
        """三档同名（official+repo）：节点 skill_id 指向 repo 档 → 覆盖分支
        入队 skill_id 命中 repo 档（不落 official）。"""
        sname = f"ovr979_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc979o_{uuid.uuid4().hex[:8]}"
        try:
            sid_o = _insert_scope_skill(sname, "official", "")
            sid_repo = _insert_scope_skill(sname, "repo", _TEST_REPO)
            self._seed_scene(scene_id, [
                {"id": "vod", "tag": "vod", "skill": sname, "skill_id": sid_repo,
                 "enabled": True}])
            self._seed_issue_with_scene(scene_id, 0)
            captured = _run_hook(_make_issue_note_raw(700), {"X-GitCode-Event": "Note Hook"})
            assert captured, "issue 场景覆盖应派发 vod 任务"
            for kw in captured:
                assert kw.get("skill_id") == sid_repo, (
                    f"覆盖分支应权威命中 repo 档 {sid_repo}, got {kw.get('skill_id')}")
                assert kw.get("skill_id") != sid_o
                assert kw.get("skill_path") == f"/skills/{sname}"
        finally:
            _delete_skills(sname)

    def test_issue_scene_user_skill_id_still_blocked(self):
        """#1758：显式 user 档 skill_id 不再视为授权，派发门禁 fail-fast 拦截。"""
        sname = f"ovr979g_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc979g_{uuid.uuid4().hex[:8]}"
        try:
            sid_u2 = _insert_scope_skill(sname, "user", "999999")
            self._seed_scene(scene_id, [
                {"id": "vod", "tag": "vod", "skill": sname, "skill_id": sid_u2,
                 "enabled": True}])
            self._seed_issue_with_scene(scene_id, 0)
            captured = _run_hook(_make_issue_note_raw(700), {"X-GitCode-Event": "Note Hook"})
            assert not captured, "user 档 skill_id 仍需 scope 校验，不应入队"
            with db_lock:
                failed = db.cursor().execute(
                    "SELECT COUNT(*) FROM events WHERE repo_key=? "
                    "AND event_type='skill_resolve_failed'", (_TEST_REPO,)).fetchone()[0]
            assert failed >= 1
        finally:
            _delete_skills(sname)


class TestUserMarketSceneNodeLookup:
    """QA④：get_node_by_tag 的 user_id 生效——个人场景（scene_market
    scope='user'）节点可查到，不再静默走降级。"""

    def test_get_node_by_tag_user_market_scene(self):
        ua = _mk_user()
        sname = f"umk979_{uuid.uuid4().hex[:6]}"
        scene_id = f"sc979u_{uuid.uuid4().hex[:8]}"
        rk = "gitcode:org/test979u"
        try:
            sid_u = _insert_scope_skill(sname, "user", str(ua["id"]))
            nodes = [{"id": "n1", "tag": "issue", "skill": sname,
                      "skill_id": sid_u, "enabled": True}]
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO scene_market (name, scope, source, source_user_id, nodes_json, "
                    "is_active, repo_key) VALUES (?, 'user', 'user', ?, ?, 1, '')",
                    (scene_id, ua["id"], json.dumps(nodes)))
                db.commit()
            # 传 user_id → 命中个人场景节点
            node = get_node_by_tag(scene_id, "issue", repo_key=rk, user_id=ua["id"])
            assert node and node.get("skill_id") == sid_u
            got = event_pipeline.resolve_scene_node_skill_id(
                rk, "issue", scene_id=scene_id, user_id=ua["id"])
            assert got == sid_u
            # 不传 user_id → 个人场景查不到（回退 pipelines.json 无此节点）
            node2 = get_node_by_tag(scene_id, "issue", repo_key=rk)
            assert node2 is None or node2.get("skill_id") != sid_u
        finally:
            _delete_skills(sname)
            with db_lock:
                db.cursor().execute("DELETE FROM scene_market WHERE name=?", (scene_id,))
                db.commit()
            _cleanup_user(ua["username"])


class TestMarketBackfillRepoScope:
    """QA③：backfill 对 scene_market scope='repo' 行按行内 repo_key 解析 repo 档
    （此前空 repo_key 漏 repo 档）。"""

    def test_market_repo_row_backfills_repo_scope_skill(self):
        sname = f"mkt979_{uuid.uuid4().hex[:6]}"
        rk = "gitcode:org/test979m"
        scene_name = f"mk979_{uuid.uuid4().hex[:8]}"
        try:
            sid_r = _insert_scope_skill(sname, "repo", rk)
            nodes = [{"id": "n1", "tag": "qa", "skill": sname, "enabled": True}]
            with db_lock:
                db.cursor().execute(
                    "INSERT INTO scene_market (name, scope, source_user_id, repo_key, "
                    "nodes_json, is_active) VALUES (?, 'repo', 0, ?, ?, 1)",
                    (scene_name, rk, json.dumps(nodes)))
                db.commit()
            _fn_scene_nodes_skill_id_backfill()
            with db_lock:
                row = db.cursor().execute(
                    "SELECT nodes_json FROM scene_market WHERE name=?", (scene_name,)).fetchone()
            mig = json.loads(row[0])
            assert mig[0].get("skill_id") == sid_r, (
                "scene_market repo 行应按行内 repo_key 命中 repo 档")
        finally:
            _delete_skills(sname)
            with db_lock:
                db.cursor().execute("DELETE FROM scene_market WHERE name=?", (scene_name,))
                db.commit()
