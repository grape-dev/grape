"""Issue #658/#3396：引擎角色标签 × 节点角色（agent_type）硬过滤回归测试。

Issue #3396 口径统一：角色守卫单一事实源 = engine_capabilities（UI 勾选即生效）；
空标签 fail-closed（A7）；注册/本机发现自动补 'all' 的存量引擎行为不变（A8）；
role_tags 注册声明列仅透传，守卫不再消费（A4）。

反例（Issue #90）：vod 节点任务不得被只勾了 coding 的引擎派发/认领。
"""

import hashlib
import secrets

import pytest

from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import engine_select as es
from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES
from src.domains.runtime.engine import service


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    _sql = (
        "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:b658/%' OR repo_key LIKE 'owner/repo'",
        "DELETE FROM events WHERE repo_key LIKE 'gitcode:b658/%' OR repo_key LIKE 'owner/repo'",
        "DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'owner/repo'",
        "DELETE FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b658-%')",
        "DELETE FROM engine_capabilities WHERE runtime_id IN "
        "(SELECT id FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b658-%'))",
        "DELETE FROM runtime_credentials WHERE name LIKE 'b658-%'",
        "DELETE FROM agent_runtimes WHERE credential_id=0",
        "DELETE FROM user_repos WHERE repo_key LIKE 'gitcode:owner/repo'",
        "DELETE FROM unified_user_mapping",
    )
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()
    # Issue #3584：同负载并列按进程内轮转序号取模，复位保证多引擎断言（min id）确定
    es._PICK_ROTATION_POOL.reset()
    yield
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, role_tags="", status="online", healthy=1,
               installed=1, scope="global", owner_id="", cap_tags=None,
               tags_user_set=0) -> int:
    """建引擎行 + 写角色标签（engine_capabilities，Issue #3396 单一事实源）。

    cap_tags=None → 自动补 'all'（对齐注册/本机发现的存量行为，A8）；
    cap_tags=[] → 手动清空（fail-closed，A7）；cap_tags=["coding"] → 勾了 coding。
    role_tags 仅注册透传（守卫不再消费）。
    """
    # 新基线：远端凭据引擎需带可信执行器探活（probe_status=ok + worker_version）
    # 才能过 claim_by_tag 的 9491 幽灵引擎闸门。
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id, role_tags, tags_user_set, probe_status, worker_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, _now(), scope, owner_id,
             role_tags, tags_user_set, "ok", "1.0.0"))
        rid = c.lastrowid
        tags = ["all"] if cap_tags is None else list(cap_tags)
        for t in tags:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                (rid, t))
        db.commit()
        return rid


def _mk_scene(scene_id, agent_type, engine_ids):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) VALUES (1, 'gitcode:owner/repo', 'owner', 'repo', 1)")
        except Exception:
            pass
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key="owner/repo", nodes=nodes)


# ===== engine_role_tags 解析（注册透传轨，守卫不再消费）=====

class TestEngineRoleTags:
    def test_no_type_default_all_unrestricted(self):
        # #658 修订：已知类型不再有默认角色映射
        assert es.engine_role_tags("opencode") is None
        assert es.engine_role_tags("codearts") is None
        assert es.engine_role_tags("hermes") is None
        assert es.engine_role_tags("codex") is None

    def test_explicit_overrides_default(self):
        tags = '["vod"]'
        assert es.engine_role_tags("codearts", tags) == ["vod"]
        assert es.engine_role_tags("codearts", '["VOD", " Chat "]') == ["vod", "chat"]

    def test_invalid_json_falls_back(self):
        # 声明非法 JSON 时按未声明处理 → None（注册语义）
        assert es.engine_role_tags("hermes", "not-json") is None


# ===== engine_capability_tags / engine_allows_role（Issue #3396 单一事实源）=====

class TestCapabilityTags:
    def test_capability_tags_read_from_db(self):
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-cap-oc"), cap_tags=["coding", "chat"])
        assert sorted(es.engine_capability_tags(oc)) == ["chat", "coding"]

    def test_capability_tags_invalid_runtime(self):
        assert es.engine_capability_tags(0) == []
        assert es.engine_capability_tags(None) == []

    def test_allows_role_membership_and_all(self):
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-cap-oc2"), cap_tags=["coding"])
        assert es.engine_allows_role(oc, "coding") is True
        assert es.engine_allows_role(oc, "vod") is False
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-cap-hm"), cap_tags=["all"])
        assert es.engine_allows_role(hm, "vod") is True
        assert es.engine_allows_role(hm, "chat") is True

    def test_empty_tags_fail_closed(self):
        # A7：手动清空标签 → 任何角色均不可调度（fail-closed）
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cap-cx"), cap_tags=[], tags_user_set=1)
        assert es.engine_capability_tags(cx) == []
        assert es.engine_allows_role(cx, "coding") is False
        assert es.engine_allows_role(cx, "sre") is False

    def test_dirty_role_tags_not_consumed_by_guard(self):
        # A4：role_tags 列改值不再影响判定（守卫只读 engine_capabilities）
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-cap-oc3"),
                        cap_tags=["all"], role_tags='["sre"]')
        assert es.engine_allows_role(oc, "vod") is True
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cap-cx2"),
                        cap_tags=["vod"], role_tags='["coding"]')
        assert es.engine_allows_role(cx, "vod") is True
        assert es.engine_allows_role(cx, "coding") is False


# ===== pick_engine 角色硬过滤（legacy 派发）=====

class TestPickEngineRoleFilter:
    def test_issue90_vod_not_dispatched_to_coding_only(self):
        """#90 反例（#3396 口径）：只勾了 coding 的引擎绑到 vod 节点 → 无候选 → None。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca"), cap_tags=["coding"])
        _mk_scene("test-658-neg", "vod", [ca])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-neg") is None

    def test_vod_dispatched_to_all_tagged_opencode(self):
        """注册默认补 'all' 的 opencode（A8 存量口径）可服务 vod。"""
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-oc"))
        _mk_scene("test-658-oc", "vod", [oc])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-oc") == oc

    def test_vod_matches_hermes(self):
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm"), cap_tags=["vod"])
        _mk_scene("test-658-hm", "vod", [hm])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-hm") == hm

    def test_coding_matches_codearts_and_opencode(self):
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca2"), cap_tags=["coding"])
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-oc2"), cap_tags=["coding"])
        _mk_scene("test-658-cd", "coding", [oc, ca])
        assert es.pick_engine("owner/repo", "coding", scene_id="test-658-cd") == min(oc, ca)

    def test_coding_only_excluded_vod_keeps_hermes(self):
        """混合候选：只勾 coding 的被过滤剔除，勾 vod 的 hermes 命中。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca3"), cap_tags=["coding"])
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm3"), cap_tags=["vod"])
        _mk_scene("test-658-mix", "vod", [ca, hm])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-mix") == hm

    def test_explicit_vod_tag_lets_codearts_serve_vod(self):
        """UI 勾选 vod 后 codearts 可服务 vod（engine_capabilities 即生效）。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca4"), cap_tags=["vod"])
        _mk_scene("test-658-rt", "vod", [ca])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-rt") == ca

    def test_unknown_engine_type_with_all_not_excluded(self):
        """未知引擎类型（勾了 all）不被角色过滤排除。"""
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cx"), cap_tags=["all"])
        _mk_scene("test-658-cx", "vod", [cx])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-cx") == cx

    def test_cleared_engine_fail_closed_dispatch(self):
        """A7：手动清空标签（tags_user_set=1 且无 capabilities 行）→ 派发侧无候选。"""
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cx7"), cap_tags=[], tags_user_set=1)
        _mk_scene("test-658-fc", "vod", [cx])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-fc") is None

    def test_ui_tag_change_takes_effect_without_restart(self):
        """A4：改标签（engine_capabilities）后无需重启/重注册，派发即时生效。"""
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-oc9"), cap_tags=["all"])
        _mk_scene("test-658-ui", "vod", [oc])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-ui") == oc
        # 模拟 UI 清空标签（PUT /tags 语义：删行 + tags_user_set=1）
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (oc,))
            c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (oc,))
            db.commit()
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-ui") is None


# ===== get_scene_pool_engine_ids 角色硬过滤（plugin 广播收口）=====

class TestScenePoolRoleFilter:
    def test_issue90_vod_coding_only_pool_none(self):
        """#90 反例：场景 vod 节点绑定只勾 coding 的引擎 → 无候选 → None。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca5"), cap_tags=["coding"])
        _mk_scene("test-658-pool-neg", "vod", [ca])
        assert es.get_scene_pool_engine_ids("test-658-pool-neg", "vod",
                                            repo_key="owner/repo", user_id=1) is None

    def test_vod_hermes_pool(self):
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm5"), cap_tags=["vod"])
        _mk_scene("test-658-pool-hm", "vod", [hm])
        import json
        assert json.loads(es.get_scene_pool_engine_ids("test-658-pool-hm", "vod",
                                                      repo_key="owner/repo", user_id=1)) == [hm]

    def test_coding_codearts_pool(self):
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca6"), cap_tags=["coding"])
        _mk_scene("test-658-pool-ca", "coding", [ca])
        import json
        assert json.loads(es.get_scene_pool_engine_ids("test-658-pool-ca", "coding",
                                                       repo_key="owner/repo", user_id=1)) == [ca]

    def test_cleared_engine_pool_none(self):
        """A7：手动清空标签 → 广播池无候选（role_no_candidates 留痕后 None）。"""
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cx6"), cap_tags=[], tags_user_set=1)
        _mk_scene("test-658-pool-fc", "vod", [cx])
        assert es.get_scene_pool_engine_ids("test-658-pool-fc", "vod",
                                            repo_key="owner/repo", user_id=1) is None


# ===== 注册显式 role_tags（/api/runtime/register）=====

class TestRegisterRoleTags:
    def _client(self):
        return TestClient(__import__("src.api.main", fromlist=["app"]).app)

    def _make_token(self, name):
        token = "rta_" + secrets.token_urlsafe(32)
        with db_lock:
            db.cursor().execute(
                "INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?, 'global')",
                (hashlib.sha256(token.encode()).hexdigest(), name))
            db.commit()
        return token

    def test_register_with_role_tags(self):
        client = self._client()
        token = self._make_token("b658-reg-1")
        r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                        json={"device_name": "b658-dev1",
                              "runtimes": [{"name": "codearts", "role_tags": ["vod", "chat"]}]})
        assert r.status_code == 200, r.text
        rid = r.json()["runtimes"][0]["id"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT role_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        import json
        assert json.loads(row[0]) == ["vod", "chat"]

    def test_register_update_keeps_manual_role_tags(self):
        """再次注册未带 role_tags → 不覆盖已有声明。"""
        client = self._client()
        token = self._make_token("b658-reg-2")
        r1 = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                         json={"device_name": "b658-dev2",
                               "runtimes": [{"name": "hermes", "role_tags": ["sre"]}]})
        rid = r1.json()["runtimes"][0]["id"]
        r2 = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                         json={"device_name": "b658-dev2",
                               "runtimes": [{"name": "hermes"}]})
        assert r2.status_code == 200
        with db_lock:
            row = db.cursor().execute(
                "SELECT role_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert row[0] == '["sre"]'

    def test_register_auto_all_keeps_legacy_engines_claimable(self):
        """A8 存量回归：新注册引擎自动补 'all' 标签 → 任意角色任务可认领（行为与基线一致）。"""
        client = self._client()
        token = self._make_token("b658-reg-3")
        r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                        json={"device_name": "b658-dev3",
                              "runtimes": [{"name": "opencode"}]})
        assert r.status_code == 200, r.text
        rid = r.json()["runtimes"][0]["id"]
        with db_lock:
            row = db.cursor().execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,)).fetchall()
        _tags = [x[0] for x in row]
        # 新基线注册会额外写 engine:*/scope:* 描述性能力行，角色标签仍必须落到 'all'
        _role_tags = [t for t in _tags if t == "all" or t in KNOWN_AGENT_ROLES]
        assert _role_tags == ["all"], f"注册必须自动补 'all'（A8 存量口径），实际 {_tags}"
        # 新基线：注册后的探活由 worker 心跳上报写入（probe_status=pending 期间认领闸门
        # 会拒领，9491 保护）；此处模拟一次探活通过，聚焦 A8 的标签口径。
        with db_lock:
            db.cursor().execute(
                "UPDATE agent_runtimes SET probe_status='ok', worker_version='1.0.0' WHERE id=?",
                (rid,))
            db.commit()
        task = service.enqueue_task(
            agent_session_id="sess-b658-a8", runtime_id=0, prompt="p",
            event_seq=65811, issue_id="#658", repo_key="gitcode:b658/repo",
            agent_type="vod", skill="issue", pool_agent_type="vod", pool_tag="vod")
        got = service.claim_by_tag("all", rid)
        assert got and got["id"] == task


# ===== claim_by_tag 认领侧角色守卫（engine_capabilities 口径）=====

class TestClaimByTagRoleGuard:
    def _enqueue_pool_task(self, tid, pool_tag, event_seq, repo_key="gitcode:b658/repo"):
        return service.enqueue_task(
            agent_session_id=f"sess-{tid}", runtime_id=0, prompt="p",
            event_seq=event_seq, issue_id="#658", repo_key=repo_key,
            agent_type=pool_tag, skill="issue", pool_agent_type=pool_tag,
            pool_tag=pool_tag)

    def test_coding_only_engine_cannot_claim_by_all(self):
        """只勾 coding 的引擎无 'all' 能力 → 按 'all' 认领被拒（标签能力匹配 fail-closed）。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca"),
                        cap_tags=["coding"], role_tags='["all"]')
        task = self._enqueue_pool_task("a", "vod", 65801)
        assert service.claim_by_tag("all", ca) is None
        assert service.get_task(task)["status"] == "queued"

    def test_all_tagged_engine_claims_vod_task(self):
        """勾了 all 的引擎可认领任意角色任务（含脏 role_tags 不影响，A4）。"""
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-cl-hm"),
                        cap_tags=["all"], role_tags='["sre"]')
        task = self._enqueue_pool_task("b", "vod", 65802)
        got = service.claim_by_tag("all", hm)
        assert got and got["id"] == task

    def test_coding_engine_claims_coding_task_by_exact_tag(self):
        """只勾 coding 的引擎按 tag='coding' 精确认领 coding 任务。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca2"), cap_tags=["coding"])
        task = self._enqueue_pool_task("c", "coding", 65803)
        got = service.claim_by_tag("coding", ca)
        assert got and got["id"] == task

    def test_role_mismatch_queue_keeps_other_tasks(self):
        """队首 vod 任务不被只勾 coding 的引擎认领，队尾 coding 任务仍可被认领。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca3"), cap_tags=["coding"])
        vod_task = self._enqueue_pool_task("d", "vod", 65804)
        coding_task = self._enqueue_pool_task("e", "coding", 65805)
        got = service.claim_by_tag("coding", ca)
        assert got and got["id"] == coding_task, "跳过角色不匹配任务，认领下一个匹配任务"
        assert service.get_task(vod_task)["status"] == "queued"

    def test_bound_claim_role_mismatch_rejected(self):
        """bound claim（expected）：引擎标签不含目标任务角色 → fail-closed 拒绝。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca4"), cap_tags=["coding"])
        task = self._enqueue_pool_task("f", "vod", 65806)
        got = service.claim_by_tag("coding", ca, expected_task_id=task, expected_event_seq=65806)
        assert got is None
        assert service.get_task(task)["status"] == "queued"

    def test_unknown_type_engine_with_all_claims_any(self):
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cl-cx"), cap_tags=["all"])
        task = self._enqueue_pool_task("g", "vod", 65807)
        got = service.claim_by_tag("all", cx)
        assert got and got["id"] == task, "勾了 all 的未知类型引擎可认领任意角色任务"

    def test_cleared_engine_cannot_claim(self):
        """A7 认领侧：手动清空标签 → 任何角色任务均不可认领（fail-closed）。"""
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cl-cx2"),
                        cap_tags=[], tags_user_set=1)
        task = self._enqueue_pool_task("h", "vod", 65808)
        assert service.claim_by_tag("all", cx) is None
        assert service.claim_by_tag("vod", cx) is None
        assert service.get_task(task)["status"] == "queued"

    def test_ui_tag_change_claim_takes_effect_without_restart(self):
        """A4 认领侧：UI 清空标签后无需重注册，认领即时失效。"""
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-cl-oc"), cap_tags=["all"])
        task1 = self._enqueue_pool_task("i", "vod", 65809)
        got = service.claim_by_tag("all", oc)
        assert got and got["id"] == task1
        # 模拟 UI 清空标签（PUT /tags 语义）
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (oc,))
            c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (oc,))
            db.commit()
        task2 = self._enqueue_pool_task("j", "vod", 65810)
        assert service.claim_by_tag("all", oc) is None
        assert service.get_task(task2)["status"] == "queued"
