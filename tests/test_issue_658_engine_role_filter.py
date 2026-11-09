"""Issue #658：引擎角色（role_tags）× 节点角色（agent_type）硬过滤回归测试。

反例（Issue #90）：vod 节点任务不得被派给 codearts 类引擎（rc=1 根因）。
正向：coding → opencode/codearts；vod → hermes。
双轨：显式 role_tags 声明 > 引擎类型默认映射兜底；未知类型零配置不被排除。
"""
import hashlib
import secrets

import pytest

from fastapi.testclient import TestClient

from src.session_manager import db, db_lock
from src.runtime import engine_select as es
from src.runtime import service


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
               installed=1, scope="global", owner_id="") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id, role_tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, _now(), scope, owner_id, role_tags))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
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


# ===== engine_role_tags 解析（双轨）=====

class TestEngineRoleTags:
    def test_no_type_default_all_unrestricted(self):
        # #658 修订：已知类型不再有默认角色映射，默认不限角色
        assert es.engine_role_tags("opencode") is None
        assert es.engine_role_tags("codearts") is None
        assert es.engine_role_tags("hermes") is None
        assert es.engine_role_tags("codex") is None

    def test_explicit_overrides_default(self):
        tags = '["vod"]'
        assert es.engine_role_tags("codearts", tags) == ["vod"]
        assert es.engine_role_tags("codearts", '["VOD", " Chat "]') == ["vod", "chat"]

    def test_invalid_json_falls_back(self):
        # 声明非法 JSON 时按未声明处理 → None（不限角色）
        assert es.engine_role_tags("hermes", "not-json") is None


# ===== pick_engine 角色硬过滤（legacy 派发）=====

class TestPickEngineRoleFilter:
    def test_issue90_vod_not_dispatched_to_declared_coding_only(self):
        """#90 反例（修订口径）：显式声明 role_tags=['coding'] 的引擎绑到 vod 节点 → 无候选 → None。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca"),
                        role_tags='["coding"]')
        _mk_scene("test-658-neg", "vod", [ca])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-neg") is None

    def test_vod_dispatched_to_undeclared_opencode(self):
        """修订口径：未声明 role_tags 的 opencode 默认不限角色，可服务 vod。"""
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-oc"))
        _mk_scene("test-658-oc", "vod", [oc])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-oc") == oc

    def test_vod_matches_hermes(self):
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm"))
        _mk_scene("test-658-hm", "vod", [hm])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-hm") == hm

    def test_coding_matches_codearts_and_opencode(self):
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca2"))
        oc = _mk_engine("opencode", credential_id=_mk_cred("b658-oc2"))
        _mk_scene("test-658-cd", "coding", [oc, ca])
        assert es.pick_engine("owner/repo", "coding", scene_id="test-658-cd") == min(oc, ca)

    def test_declared_coding_excluded_vod_keeps_hermes(self):
        """混合候选（修订口径）：显式声明 coding-only 的被过滤剔除，未声明的 hermes 命中。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca3"),
                        role_tags='["coding"]')
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm3"))
        _mk_scene("test-658-mix", "vod", [ca, hm])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-mix") == hm

    def test_explicit_role_tags_lets_codearts_serve_vod(self):
        """显式 role_tags 覆盖默认映射：codearts 声明 ["vod"] 后可服务 vod。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca4"),
                        role_tags='["vod"]')
        _mk_scene("test-658-rt", "vod", [ca])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-rt") == ca

    def test_unknown_engine_type_not_excluded(self):
        """未知引擎类型（未声明 role_tags）不被角色过滤排除（零配置兼容）。"""
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cx"))
        _mk_scene("test-658-cx", "vod", [cx])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-658-cx") == cx


# ===== get_scene_pool_engine_ids 角色硬过滤（plugin 广播收口）=====

class TestScenePoolRoleFilter:
    def test_issue90_vod_declared_coding_pool_none(self):
        """#90 反例（修订口径）：场景 vod 节点绑定显式 coding-only 引擎 → 无候选 → None。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca5"),
                        role_tags='["coding"]')
        _mk_scene("test-658-pool-neg", "vod", [ca])
        assert es.get_scene_pool_engine_ids("test-658-pool-neg", "vod",
                                            repo_key="owner/repo", user_id=1) is None

    def test_vod_hermes_pool(self):
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-hm5"))
        _mk_scene("test-658-pool-hm", "vod", [hm])
        import json
        assert json.loads(es.get_scene_pool_engine_ids("test-658-pool-hm", "vod",
                                                      repo_key="owner/repo", user_id=1)) == [hm]

    def test_coding_codearts_pool(self):
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-ca6"))
        _mk_scene("test-658-pool-ca", "coding", [ca])
        import json
        assert json.loads(es.get_scene_pool_engine_ids("test-658-pool-ca", "coding",
                                                      repo_key="owner/repo", user_id=1)) == [ca]


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


# ===== claim_by_tag 认领侧角色守卫（堵全量广播漏洞）=====

class TestClaimByTagRoleGuard:
    def _enqueue_pool_task(self, tid, pool_tag, event_seq, repo_key="gitcode:b658/repo"):
        return service.enqueue_task(
            agent_session_id=f"sess-{tid}", runtime_id=0, prompt="p",
            event_seq=event_seq, issue_id="#658", repo_key=repo_key,
            agent_type=pool_tag, skill="issue", pool_agent_type=pool_tag,
            pool_tag=pool_tag)

    def test_declared_coding_only_cannot_claim_vod_task(self):
        """#90 反例（广播队列，修订口径）：显式声明 coding-only 的引擎不得认领 vod 任务。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca"),
                        role_tags='["coding"]')
        task = self._enqueue_pool_task("a", "vod", 65801)
        assert service.claim_by_tag("all", ca) is None
        assert service.get_task(task)["status"] == "queued"

    def test_hermes_claims_vod_task(self):
        hm = _mk_engine("hermes", credential_id=_mk_cred("b658-cl-hm"))
        task = self._enqueue_pool_task("b", "vod", 65802)
        got = service.claim_by_tag("all", hm)
        assert got and got["id"] == task

    def test_codearts_claims_coding_task(self):
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca2"))
        task = self._enqueue_pool_task("c", "coding", 65803)
        got = service.claim_by_tag("all", ca)
        assert got and got["id"] == task

    def test_role_guard_skips_mismatch_keeps_queue(self):
        """队首 vod 任务被显式 coding-only 引擎跳过后，队尾 coding 任务仍可被认领。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca3"),
                        role_tags='["coding"]')
        vod_task = self._enqueue_pool_task("d", "vod", 65804)
        import datetime
        _old = (datetime.datetime.now(datetime.timezone.utc)).isoformat()
        coding_task = self._enqueue_pool_task("e", "coding", 65805)
        got = service.claim_by_tag("all", ca)
        assert got and got["id"] == coding_task, "跳过角色不匹配任务，认领下一个匹配任务"
        assert service.get_task(vod_task)["status"] == "queued"

    def test_bound_claim_role_mismatch_rejected(self):
        """bound claim（expected）：显式声明角色不匹配 → fail-closed 拒绝。"""
        ca = _mk_engine("codearts", credential_id=_mk_cred("b658-cl-ca4"),
                        role_tags='["coding"]')
        task = self._enqueue_pool_task("f", "vod", 65806)
        got = service.claim_by_tag("all", ca, expected_task_id=task, expected_event_seq=65806)
        assert got is None
        assert service.get_task(task)["status"] == "queued"

    def test_unknown_type_engine_claims_any(self):
        cx = _mk_engine("codex", credential_id=_mk_cred("b658-cl-cx"))
        task = self._enqueue_pool_task("g", "vod", 65807)
        got = service.claim_by_tag("all", cx)
        assert got and got["id"] == task, "未知类型引擎不受角色限制（零配置兼容）"
