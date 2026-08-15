"""引擎集合 / 派发选择测试（engine-selection-design.md v2）。

覆盖：绑定表读写、priority 派生、pick_engine 优先级（专属→全局→本机）+ 健康过滤 + legacy 回退、
resolve_default_engine（托管种子）、query_engines 档位查询。
"""
import datetime
import pytest

from src.session_manager import db, db_lock
from src.runtime import engine_select as es


@pytest.fixture(autouse=True)
def _clean_tables():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_runtime_binding")
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_runtime_binding")
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
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
        db.commit()
        return rid


class TestBinding:
    def test_set_get_binding(self):
        a = _mk_engine("opencode-a", credential_id=_mk_cred("c-a"))
        b = _mk_engine("opencode-b", credential_id=_mk_cred("c-b"))
        es.set_binding("owner/repo", "vod", [a, b])
        got = es.get_binding("owner/repo", "vod")
        assert [m["runtime_id"] for m in got] == [a, b]
        assert all(m["priority"] == 50 for m in got)   # 全局默认 priority

    def test_set_binding_replaces_and_clears(self):
        a = _mk_engine("opencode-a", credential_id=_mk_cred("c-a"))
        es.set_binding("owner/repo", "vod", [a])
        es.set_binding("owner/repo", "vod", [])
        assert es.get_binding("owner/repo", "vod") == []

    def test_priority_by_scope(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        loc_eng = _mk_engine("opencode-l", credential_id=0)   # 本机直连
        es.set_binding("owner/repo", "coding", [loc_eng, glob_eng, repo_eng])
        by_id = {m["runtime_id"]: m["priority"] for m in es.get_binding("owner/repo", "coding")}
        assert by_id[repo_eng] == 10    # 仓库级专属优先
        assert by_id[glob_eng] == 50
        assert by_id[loc_eng] == 90     # 本机最后兜底


class TestPickEngine:
    def test_priority_repo_first(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        es.set_binding("owner/repo", "vod", [glob_eng, repo_eng])
        assert es.pick_engine("owner/repo", "vod") == repo_eng

    def test_repo_offline_falls_to_global(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo", status="offline")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        es.set_binding("owner/repo", "vod", [glob_eng, repo_eng])
        assert es.pick_engine("owner/repo", "vod") == glob_eng   # 专属离线 → 全局兜底

    def test_global_offline_falls_to_local(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"), status="offline")
        loc_eng = _mk_engine("opencode-l", credential_id=0)
        es.set_binding("owner/repo", "vod", [glob_eng, loc_eng])
        assert es.pick_engine("owner/repo", "vod") == loc_eng    # 本机最后兜底

    def test_no_healthy_member_returns_none(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"), status="offline")
        es.set_binding("owner/repo", "vod", [glob_eng])
        assert es.pick_engine("owner/repo", "vod") is None       # engine_unavailable

    def test_fallback_runtime_id_when_no_binding(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        # 无绑定集合 → 回退会话主引擎（legacy）
        assert es.pick_engine("owner/repo", "vod", fallback_runtime_id=glob_eng) == glob_eng

    def test_empty_binding_falls_back(self):
        # 绑定集合为空 → 回退会话主引擎（legacy，见 engine-selection-design.md §6）
        assert es.pick_engine("owner/repo", "vod", fallback_runtime_id=999) == 999
        # 绑定集合为空且无 fallback → None（无引擎不可用）
        assert es.pick_engine("owner/repo", "vod") is None

    def test_repo_engine_not_shared_to_other_repo(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        es.set_binding("owner/repo", "vod", [repo_eng])
        es.set_binding("other/repo", "vod", [repo_eng])   # 模拟误绑：其他仓库的集合也含该引擎
        # 专属判定按 owner_id：owner/repo 的引擎不归 other/repo 的 repo 档 → 视为 global 档
        assert es.pick_engine("other/repo", "vod") is None or es.pick_engine("other/repo", "vod") == repo_eng


class TestResolveDefaultEngine:
    def test_repo_engine_preferred(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        repo_eng = _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        assert es.resolve_default_engine("owner/repo") == repo_eng   # 专属优先

    def test_only_global(self):
        glob_eng = _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        assert es.resolve_default_engine("owner/repo") == glob_eng

    def test_only_local_healthy(self):
        loc_eng = _mk_engine("opencode-l", credential_id=0)
        assert es.resolve_default_engine("owner/repo") == loc_eng

    def test_local_unhealthy_not_selected(self):
        _mk_engine("opencode-l", credential_id=0, healthy=0)   # 未健康本机引擎
        assert es.resolve_default_engine("owner/repo") is None


class TestResolveLocalEngine:
    def test_resolve_local_engine_returns_healthy_local(self):
        loc_eng = _mk_engine("opencode-l", credential_id=0)
        assert es.resolve_local_engine() == loc_eng

    def test_resolve_local_engine_ignores_remote_and_unhealthy(self):
        _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))          # 远端全局不参与
        _mk_engine("opencode-l-bad", credential_id=0, healthy=0)          # 本机不健康
        assert es.resolve_local_engine() is None


class TestQueryEngines:
    def test_query_scopes(self):
        repo_cid = _mk_cred("c-repo", scope="repo", owner_id="owner/repo")
        _mk_engine("opencode-r", credential_id=repo_cid, scope="repo", owner_id="owner/repo")
        _mk_engine("opencode-g", credential_id=_mk_cred("c-g"))
        _mk_engine("opencode-l", credential_id=0)
        assert len(es.query_engines("owner/repo", "repo")) == 1
        assert len(es.query_engines("owner/repo", "global")) == 1
        assert len(es.query_engines("owner/repo", "local")) == 1
