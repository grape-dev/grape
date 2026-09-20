"""引擎选择 / 派发测试（场景 engine_runtime_ids 模型）。

覆盖：pick_engine 优先级（专属→全局→本机）+ 健康过滤 + legacy 回退、
resolve_default_engine（托管种子）、resolve_local_engine、query_engines 档位查询。
"""
import datetime
import json
import pytest

from src.session_manager import db, db_lock
from src.runtime import engine_select as es


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    # 远端 a1f4a450 将默认 pipeline mode 改为 plugin（pick_engine 统一返回 0 广播）。
    # TestPickEngine 测的是 legacy 模式的优先级逻辑，需强制 legacy。
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM devops_agent_session")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_pipeline_scene")
        c.execute("DELETE FROM unified_user_mapping")
        c.execute("DELETE FROM user_repos")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM devops_agent_session")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_pipeline_scene")
        c.execute("DELETE FROM unified_user_mapping")
        c.execute("DELETE FROM user_repos")
        db.commit()


def _now():
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


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", owner_id="", last_seen=None) -> int:
    ls = last_seen if last_seen is not None else _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, ls, scope, owner_id))
        rid = c.lastrowid
        # engine_capabilities tag="all" — _filter_by_tags 需要至少一条匹配记录否则引擎被过滤掉
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
        db.commit()
        return rid


def _mk_scene(scene_id, agent_type, engine_ids, scope="user", user_id=1, repo_key=""):
    """Create a scene with a single node containing engine_runtime_ids.

    Issue #39 方案 B 后 devops_pipeline_scene 表移除了 scope/user_id 列，
    场景仅按 repo_key 关联。测试中用 repo_key="owner/repo" 匹配 pick_engine 的查询。
    """
    # 确保 user_repos 有记录，让 _repo_user_id 能查到
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) VALUES (?, 'gitcode:owner/repo', 'owner', 'repo', 1)", (user_id,))
        except Exception:
            pass
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    # weekly-02: create_custom_scene 签名移除 scope/user_id，场景按 repo_key 关联
    # 测试统一用 "owner/repo" 作为 repo_key，与 pick_engine("owner/repo", ...) 对齐
    _rk = repo_key or "owner/repo"
    _rk = _rk.split(":")[-1] if ":" in _rk else _rk  # 归一化去掉 gitcode: 前缀
    create_custom_scene(
        scene_id=scene_id, name=scene_id, description="",
        repo_key=_rk,
        nodes=nodes)


class TestPickEngine:
    def test_priority_repo_first(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_scene("test-pr", "vod", [glob_eng, repo_eng])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-pr") == repo_eng

    def test_repo_offline_falls_to_global(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo", status="offline")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_scene("test-ro", "vod", [glob_eng, repo_eng])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-ro") == glob_eng

    def test_global_offline_falls_to_local(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"), status="offline")
        loc_eng = _mk_engine("opencode", credential_id=0)
        _mk_scene("test-gl", "vod", [glob_eng, loc_eng])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-gl") == loc_eng

    def test_no_healthy_member_returns_none(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"), status="offline")
        _mk_scene("test-nh", "vod", [glob_eng])
        assert es.pick_engine("owner/repo", "vod", scene_id="test-nh") is None

    def test_fallback_runtime_id_when_no_scene(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        # 无 scene_id → engine_runtime_ids 为空 → 回退会话主引擎（legacy）
        assert es.pick_engine("owner/repo", "vod", fallback_runtime_id=glob_eng) == glob_eng

    def test_empty_engine_ids_falls_back(self):
        # engine_runtime_ids 为空 → 回退会话主引擎（legacy）
        _mk_scene("test-eb", "vod", [])
        assert es.pick_engine("owner/repo", "vod", fallback_runtime_id=999, scene_id="test-eb") == 999
        # engine_runtime_ids 为空且无 fallback → None
        assert es.pick_engine("owner/repo", "vod", scene_id="test-eb") is None

    def test_no_scene_no_fallback_returns_none(self):
        assert es.pick_engine("owner/repo", "vod") is None


class TestResolveDefaultEngine:
    def test_repo_engine_preferred(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        assert es.resolve_default_engine("owner/repo") == repo_eng

    def test_only_global(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        assert es.resolve_default_engine("owner/repo") == glob_eng

    def test_only_local_healthy(self):
        loc_eng = _mk_engine("opencode", credential_id=0)
        assert es.resolve_default_engine("owner/repo") == loc_eng

    def test_local_unhealthy_not_selected(self):
        _mk_engine("opencode-l", credential_id=0, healthy=0)
        assert es.resolve_default_engine("owner/repo") is None


class TestResolveLocalEngine:
    def setup_method(self):
        es._resolve_local_cache.clear()

    def test_resolve_local_engine_returns_healthy_local(self):
        loc_eng = _mk_engine("opencode", credential_id=0)
        assert es.resolve_local_engine() == loc_eng

    def test_resolve_local_engine_ignores_unhealthy_and_falls_back_global(self):
        _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_engine("opencode", credential_id=0, healthy=0)
        assert es.resolve_local_engine() is not None
        assert es.resolve_local_engine() != 0


class TestQueryEngines:
    def test_query_scopes(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_engine("opencode", credential_id=0)
        assert len(es.query_engines("owner/repo", "repo")) == 1
        assert len(es.query_engines("owner/repo", "global")) == 1
        assert len(es.query_engines("owner/repo", "local")) == 1

    def test_query_scene_tags_default(self):
        """Issue #198: query_engines 返回 scene_tags，未设置时回退 ["default"]。"""
        _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        result = es.query_engines("owner/repo", "global")
        assert len(result) == 1
        assert result[0]["scene_tags"] == ["default"]

    def test_query_scene_tags_custom(self):
        """Issue #198: query_engines 正确解析自定义 scene_tags JSON。"""
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET scene_tags=? WHERE id=?", ('["iac","cloud-native"]', glob_eng))
            db.commit()
        result = es.query_engines("owner/repo", "global")
        assert len(result) == 1
        assert result[0]["scene_tags"] == ["iac", "cloud-native"]


class TestScenePoolEngineIds:
    def test_non_empty_ids(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_scene("test-pe", "vod", [glob_eng])
        result = es.get_scene_pool_engine_ids("test-pe", "vod", repo_key="owner/repo", user_id=1)
        assert json.loads(result) == [glob_eng]

    def test_empty_ids_returns_empty(self):
        _mk_scene("test-pe2", "vod", [])
        assert es.get_scene_pool_engine_ids("test-pe2", "vod", repo_key="owner/repo", user_id=1) == ""

    def test_no_scene_returns_empty(self):
        assert es.get_scene_pool_engine_ids("nonexistent", "vod", repo_key="owner/repo", user_id=1) == ""


class TestRepoUserIdIssueCreator:
    """回归测试：_repo_user_id(repo_key, issue_creator=...) 4 场景（Issue #116）。

    PR #123 修复：6 处调用点补传 issue_creator，使多用户仓库下 _repo_user_id
    优先按 Issue 创建者反查 unified_user_mapping，而非取 user_repos 中 user_id 最小行。
    """

    REPO = "multiuser/demo"
    GITCODE_REPO = "gitcode:multiuser/demo"

    def _seed(self, users, mapping=None):
        """插入 user_repos 多用户 + 可选 unified_user_mapping 记录。"""
        with db_lock:
            c = db.cursor()
            for uid in users:
                c.execute(
                    "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                    "VALUES (?, ?, 'multiuser', 'demo', 1)",
                    (uid, self.GITCODE_REPO))
            if mapping:
                for uid, gcu in mapping:
                    c.execute(
                        "INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user) "
                        "VALUES (?, ?)",
                        (uid, gcu))
            db.commit()

    def test_issue_creator_resolved_to_mapping(self):
        """场景1：issue_creator 匹配 unified_user_mapping → 返回该 user_id（非最小 user_id）。"""
        self._seed(users=[100, 501], mapping=[(501, "creator_alice")])
        assert es._repo_user_id(self.REPO, issue_creator="creator_alice") == 501

    def test_unknown_issue_creator_falls_back(self):
        """场景2：issue_creator 未知 → 回退 user_repos ORDER BY user_id ASC（取最小）。"""
        self._seed(users=[100, 200])
        assert es._repo_user_id(self.REPO, issue_creator="ghost_user") == 100

    def test_empty_issue_creator_falls_back(self):
        """场景3：issue_creator="" → 回退 user_repos ORDER BY user_id ASC。"""
        self._seed(users=[100, 200])
        assert es._repo_user_id(self.REPO, issue_creator="") == 100

    def test_no_issue_creator_arg_falls_back(self):
        """场景4：不传 issue_creator（默认 ""）→ 回退 user_repos ORDER BY user_id ASC。"""
        self._seed(users=[100, 200])
        assert es._repo_user_id(self.REPO) == 100
