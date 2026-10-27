"""Issue #472: Runtimes 页面 Engines / Workers 统计栏全局口径（整体搜索结果）测试。

覆盖两个端点的新增能力：
- GET /api/settings/agent-runtimes：q/type/scene/tag 筛选参数（后端过滤）+ health_stats
  （筛选后全量搜索结果的健康分布，total 与四态之和一致，跨页稳定）
- GET /api/settings/runtime-devices：health_stats（scope/type/q 筛选后全量分布）

口径定义（Issue #472 验收标准）：
- 统计栏「共 N 台」= 整体搜索结果总数（非当前页条数）
- 分页切换不影响统计数字（统计与分页解耦）
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient

from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)

_TZ = _dt.timezone.utc
_ENG_PREFIX = "rt472-"
_DEV_PREFIX = "rt472dev"


def _ts(seconds_ago: float) -> str:
    return (_dt.datetime.now(_TZ) - _dt.timedelta(seconds=seconds_ago)).isoformat()


def _setup_engines():
    """插入 5 台 rt472-* 引擎（一台 global 凭据下）：3 online / 1 recently_lost / 1 offline。"""
    rows = [
        # (name, status, last_seen_seconds_ago, scene_tags, agent_tags, alias)
        ("rt472-hermes-a", "online", 10, '["dev","prod"]', '["coding"]', "alias-a"),
        ("rt472-codex-b", "online", 10, '["dev"]', '["all"]', ""),
        ("rt472-hermes-c", "online", 10, '["prod"]', '["qa"]', ""),
        ("rt472-opencode-d", "offline", 3600, '["dev"]', '["issue"]', ""),
        ("rt472-codex-e", "online", 180, '["prod"]', '["sre"]', ""),
    ]
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE ?", (_ENG_PREFIX + "%",))
        c.execute("DELETE FROM runtime_credentials WHERE name=?", ("cred-rt472",))
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("hcred-rt472", "cred-rt472", "dev-rt472", "global", "", "tester"))
        cid = c.lastrowid
        for name, status, ago, scene, tags, alias in rows:
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
                "last_seen_at, scope, owner_id, scene_tags, agent_tags, alias) VALUES (?,?,?,?,1,1,?,?,?,?,?,?)",
                (name, name, cid, status, _ts(ago), "global", "", scene, tags, alias))
        db.commit()
    return cid


def _teardown_engines(cid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE ?", (_ENG_PREFIX + "%",))
        c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _headers():
    # 复用 test_api_routes 的建号逻辑（独立测试库由 conftest/环境变量保证）
    from tests.test_api_routes import _jwt_access_token, setup_module  # noqa: F401
    from src.session_manager import db as _db, db_lock as _dl
    with _dl:
        c = _db.cursor()
        c.execute("SELECT id, username FROM grape_accounts WHERE username='test_api'")
        row = c.fetchone()
    if not row:
        setup_module()
        with _dl:
            c = _db.cursor()
            c.execute("SELECT id, username FROM grape_accounts WHERE username='test_api'")
            row = c.fetchone()
    return {"Authorization": f"Bearer {_jwt_access_token(row[0], row[1])}"}


class TestAgentRuntimesGlobalStats:
    """Issue #472: Engines 统计栏全局口径 —— health_stats 为整体搜索结果分布。"""

    def test_health_stats_total_is_global_not_page(self):
        cid = _setup_engines()
        try:
            h = _headers()
            r = client.get(f"/api/settings/agent-runtimes?page=1&page_size=2&q={_ENG_PREFIX}",
                           headers=h)
            assert r.status_code == 200
            d = r.json()
            # 当前页只有 2 条，但统计口径是整体搜索结果
            assert len(d["runtimes"]) == 2
            assert d["total"] == 5
            stats = d["health_stats"]
            assert stats["total"] == 5
            assert stats["online"] == 3
            assert stats["recently_lost"] == 1
            assert stats["offline"] == 1
            assert stats["about_to_gc"] == 0
            assert stats["online"] + stats["recently_lost"] + stats["offline"] + stats["about_to_gc"] == stats["total"]
        finally:
            _teardown_engines(cid)

    def test_health_stats_stable_across_pages(self):
        """分页切换不影响统计栏数字（统计与分页解耦）。"""
        cid = _setup_engines()
        try:
            h = _headers()
            s1 = client.get(f"/api/settings/agent-runtimes?page=1&page_size=2&q={_ENG_PREFIX}",
                            headers=h).json()["health_stats"]
            s2 = client.get(f"/api/settings/agent-runtimes?page=2&page_size=2&q={_ENG_PREFIX}",
                            headers=h).json()["health_stats"]
            s3 = client.get(f"/api/settings/agent-runtimes?page=3&page_size=2&q={_ENG_PREFIX}",
                            headers=h).json()["health_stats"]
            assert s1 == s2 == s3
        finally:
            _teardown_engines(cid)

    def test_q_filter_scopes_health_stats(self):
        """搜索条件作用于整体搜索结果（非当前页）。"""
        cid = _setup_engines()
        try:
            h = _headers()
            d = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&q={_ENG_PREFIX}hermes",
                           headers=h).json()
            assert d["total"] == 2
            assert d["health_stats"]["total"] == 2
            assert d["health_stats"]["online"] == 2
            assert all("hermes" in rt["name"] for rt in d["runtimes"])
        finally:
            _teardown_engines(cid)

    def test_type_filter_scopes_health_stats(self):
        cid = _setup_engines()
        try:
            h = _headers()
            d = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&type=codex&q={_ENG_PREFIX}",
                           headers=h).json()
            assert d["total"] == 2
            assert d["health_stats"]["total"] == 2
        finally:
            _teardown_engines(cid)

    def test_scene_filter_scopes_health_stats(self):
        cid = _setup_engines()
        try:
            h = _headers()
            d = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&scene=dev&q={_ENG_PREFIX}",
                           headers=h).json()
            # dev: hermes-a / codex-b / opencode-d
            assert d["total"] == 3
            assert d["health_stats"]["total"] == 3
            assert d["health_stats"]["online"] == 2
            assert d["health_stats"]["offline"] == 1
        finally:
            _teardown_engines(cid)

    def test_tag_filter_scopes_health_stats(self):
        cid = _setup_engines()
        try:
            h = _headers()
            d = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&tag=coding&q={_ENG_PREFIX}",
                           headers=h).json()
            assert d["total"] == 1
            assert d["health_stats"]["total"] == 1
            # 'all' 标签豁免：tag=qa 命中 codex-b(all) + hermes-c(qa)
            d2 = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&tag=qa&q={_ENG_PREFIX}",
                            headers=h).json()
            assert d2["total"] == 2
        finally:
            _teardown_engines(cid)

    def test_clear_filters_restores_full_total(self):
        """清除筛选后统计恢复全量口径（q 为空 → 全局可见引擎总数）。"""
        cid = _setup_engines()
        try:
            h = _headers()
            full = client.get("/api/settings/agent-runtimes?page=1&page_size=1", headers=h).json()
            assert full["health_stats"]["total"] == full["total"]
            assert sum(full["health_stats"][k] for k in
                       ("online", "recently_lost", "offline", "about_to_gc")) == full["total"]
        finally:
            _teardown_engines(cid)

    def test_avail_tags_from_full_visible_set(self):
        """筛选标签来自全量可见集（不随筛选/分页消失）。"""
        cid = _setup_engines()
        try:
            h = _headers()
            d = client.get(f"/api/settings/agent-runtimes?page=1&page_size=1&q={_ENG_PREFIX}hermes",
                           headers=h).json()
            for key in ("avail_scopes", "avail_types", "avail_scenes", "avail_tags"):
                assert key in d, f"缺少 {key}"
            assert "hermes" in d["avail_types"]
            assert "codex" in d["avail_types"]
            assert "dev" in d["avail_scenes"]
            assert "coding" in d["avail_tags"]
        finally:
            _teardown_engines(cid)


class TestRuntimeDevicesGlobalStats:
    """Issue #472: Workers 统计栏全局口径 —— runtime-devices health_stats。"""

    def _setup(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE ?", (_DEV_PREFIX + "-eng-%",))
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE ?", (_DEV_PREFIX + "-cred-%",))
            creds = [
                ("rt472dev-cred-1", "rt472dev-1", 10),      # online
                ("rt472dev-cred-2", "rt472dev-2", 180),     # recently_lost
                ("rt472dev-cred-3", "rt472dev-3", 3600),    # offline
            ]
            cids = []
            for cname, dev, ago in creds:
                c.execute(
                    "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status, last_seen_at) "
                    "VALUES (?,?,?,?,?,?, 'active', ?)",
                    ("h-" + cname, cname, dev, "global", "", "tester", _ts(ago)))
                cid = c.lastrowid
                cids.append(cid)
                c.execute(
                    "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
                    "VALUES (?,?,?,?,1,1,?,?, '')",
                    (_DEV_PREFIX + "-eng-" + dev, _DEV_PREFIX + "-eng-" + dev, cid, "online", _ts(ago), "global"))
            db.commit()
        return cids

    def _teardown(self, cids):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE ?", (_DEV_PREFIX + "-eng-%",))
            for cid in cids:
                c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
            db.commit()

    def test_health_stats_total_is_global_not_page(self):
        cids = self._setup()
        try:
            h = _headers()
            d = client.get(f"/api/settings/runtime-devices?page=1&page_size=1&q={_DEV_PREFIX}",
                           headers=h).json()
            assert len(d["devices"]) == 1
            assert d["total"] == 3
            stats = d["health_stats"]
            assert stats["total"] == 3
            assert stats["online"] == 1
            assert stats["recently_lost"] == 1
            assert stats["offline"] == 1
            assert stats["about_to_gc"] == 0
        finally:
            self._teardown(cids)

    def test_health_stats_stable_across_pages(self):
        cids = self._setup()
        try:
            h = _headers()
            s1 = client.get(f"/api/settings/runtime-devices?page=1&page_size=1&q={_DEV_PREFIX}",
                            headers=h).json()["health_stats"]
            s2 = client.get(f"/api/settings/runtime-devices?page=2&page_size=1&q={_DEV_PREFIX}",
                            headers=h).json()["health_stats"]
            s3 = client.get(f"/api/settings/runtime-devices?page=3&page_size=1&q={_DEV_PREFIX}",
                            headers=h).json()["health_stats"]
            assert s1 == s2 == s3
        finally:
            self._teardown(cids)

    def test_no_filter_health_stats_matches_total(self):
        cids = self._setup()
        try:
            h = _headers()
            d = client.get("/api/settings/runtime-devices?page=1&page_size=1", headers=h).json()
            assert d["health_stats"]["total"] == d["total"]
            assert sum(d["health_stats"][k] for k in
                       ("online", "recently_lost", "offline", "about_to_gc")) == d["total"]
        finally:
            self._teardown(cids)
