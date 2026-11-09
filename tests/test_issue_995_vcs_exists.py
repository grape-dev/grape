"""Issue #995：issues list VCS 存在性过滤 / include_deleted / fail-open / 跨 provider / 回填幂等。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db
from src.api.jwt_utils import create_access_token
from src.auth.grape_account_db import get_token_version
from src.session_manager import db, db_lock
import src.api.issue_vcs_index as vcs_index

client = TestClient(app)


class TestIssue995VcsExists:
    P_OWNER = "test995i"
    P_REPO = "vcsexist"
    P_REPO_KEY = f"gitcode:{P_OWNER}/{P_REPO}"
    GH_REPO_KEY = f"github:{P_OWNER}/{P_REPO}"

    def setup_class(cls):
        cls._uid = grape_account_db.create_grape_account("test995_user", email=None, password="x")
        if not cls._uid:
            cls._uid = grape_account_db.get_grape_account_by_username("test995_user")
        cls._headers = {"Authorization": "Bearer " + create_access_token(
            cls._uid["id"], cls._uid["username"], get_token_version(cls._uid["id"]))}
        try:
            from src.api import db_setup
            db_setup.init_database()
        except Exception:
            pass

    def setup_method(self):
        self._clean()
        vcs_index.clear_cache()

    def teardown_method(self):
        self._clean()
        vcs_index.clear_cache()

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key IN (?, ?)",
                      (self.P_REPO_KEY, self.GH_REPO_KEY))
            c.execute("DELETE FROM events WHERE repo_key IN (?, ?)",
                      (self.P_REPO_KEY, self.GH_REPO_KEY))
            db.commit()

    def _mk(self, number, status="processing", repo_key=None):
        _rk = repo_key or self.P_REPO_KEY
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._uid["id"], "grape_bot", _rk, number, f"Task #{number}", status))
            db.commit()

    def _get(self, qs=""):
        return client.get(f"/api/issues/{self.P_OWNER}/{self.P_REPO}{qs}", headers=self._headers)

    def _patch_numbers(self, mapping, confirm_state="missing"):
        vcs_index.clear_cache()
        import src.api.event_pipeline as event_pipeline
        original = (vcs_index.vcs_issue_numbers, event_pipeline.fetch_issue_via_vcs)
        vcs_index.vcs_issue_numbers = lambda rk: mapping.get(rk)
        event_pipeline.fetch_issue_via_vcs = (
            lambda rk, num: (confirm_state, {}) if confirm_state else ("unknown", {}))
        return original

    def _restore(self, original):
        import src.api.event_pipeline as event_pipeline
        vcs_index.vcs_issue_numbers, event_pipeline.fetch_issue_via_vcs = original
        vcs_index.clear_cache()

    def test_default_filters_deleted_and_marks_fields(self):
        for n in (1, 2, 3, 4):
            self._mk(n)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({1, 2})})
        try:
            d = self._get("?mode=time&per_page=50").json()
            nums = {it["issue_number"] for it in d["issues"]}
            assert nums == {1, 2}
            assert d["total"] == 2
            for it in d["issues"]:
                assert it["vcs_exists"] is True
                assert it["is_vcs_deleted"] is False
                assert it["vcs_check"] == "ok"
        finally:
            self._restore(original)

    def test_include_deleted_returns_flagged_cards(self):
        for n in (1, 2):
            self._mk(n)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({1})})
        try:
            d = self._get("?mode=time&per_page=50&include_deleted=1").json()
            nums = {it["issue_number"] for it in d["issues"]}
            assert nums == {1, 2}
            deleted = [it for it in d["issues"] if it["issue_number"] == 2][0]
            assert deleted["vcs_exists"] is False
            assert deleted["is_vcs_deleted"] is True
        finally:
            self._restore(original)

    def test_paged_total_and_has_more_match_filtered_set(self):
        for n in (10, 11, 12, 13, 14):
            self._mk(n)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({10, 12, 14})})
        try:
            seen = []
            page = 1
            while True:
                d = self._get(f"?mode=time&per_page=2&page={page}").json()
                assert d["total"] == 3
                seen.extend(it["issue_number"] for it in d["issues"])
                if not d["has_more"]:
                    break
                page += 1
            assert seen == [14, 12, 10]
        finally:
            self._restore(original)

    def test_fail_open_when_vcs_unavailable(self):
        for n in (20, 21):
            self._mk(n)
        original = self._patch_numbers({self.P_REPO_KEY: None})
        try:
            d = self._get("?mode=time&per_page=50").json()
            assert {it["issue_number"] for it in d["issues"]} == {20, 21}
            assert all(it["vcs_check"] == "unavailable" for it in d["issues"])
        finally:
            self._restore(original)

    def test_cross_provider_not_mixed(self):
        self._mk(1, repo_key=self.P_REPO_KEY)
        self._mk(2, repo_key=self.P_REPO_KEY)
        self._mk(3, repo_key=self.GH_REPO_KEY)
        self._mk(4, repo_key=self.GH_REPO_KEY)
        original = self._patch_numbers({
            self.P_REPO_KEY: frozenset({1}),
            self.GH_REPO_KEY: frozenset({3, 4}),
        })
        try:
            d = self._get("?mode=time&per_page=50").json()
            got = {(it["repo_key"], it["issue_number"]) for it in d["issues"]}
            assert got == {
                (self.P_REPO_KEY, 1),
                (self.GH_REPO_KEY, 3),
                (self.GH_REPO_KEY, 4),
            }
        finally:
            self._restore(original)

    def test_stale_index_does_not_hide_new_card(self):
        """索引缓存未包含新建卡片时，单条复核确认存在 → 不误标删除（Issue #995 risk）。"""
        self._mk(40)
        self._mk(41)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({40})}, confirm_state="exists")
        try:
            d = self._get("?mode=time&per_page=50").json()
            assert {it["issue_number"] for it in d["issues"]} == {40, 41}
            assert all(it["is_vcs_deleted"] is False for it in d["issues"])
        finally:
            self._restore(original)

    def test_backfill_idempotent(self):
        for n in (30, 31):
            self._mk(n)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({30})})
        try:
            s1 = vcs_index.backfill_vcs_exists([self.P_REPO_KEY])
            s2 = vcs_index.backfill_vcs_exists([self.P_REPO_KEY])
            assert s1["ok"] == 1 and s2["ok"] == 1
            with db_lock:
                c = db.cursor()
                c.execute("SELECT issue_number, vcs_exists, vcs_checked_at FROM issue "
                          "WHERE repo_key=? ORDER BY issue_number", (self.P_REPO_KEY,))
                rows = c.fetchall()
            assert [(r[0], r[1]) for r in rows] == [(30, 1), (31, 0)]
            assert all(r[2] for r in rows)
        finally:
            self._restore(original)

    def test_fetch_gitcode_short_page_not_treated_as_end(self):
        """非空短页不代表末页：分页应继续到空页，避免索引截断导致批量误删。"""
        import src.user.repo_manager as repo_manager
        pages = {
            1: [{"number": "1"}, {"number": "2"}],
            2: [{"number": "3"}],
            3: [],
        }
        calls = []
        original = repo_manager._req

        def fake_req(method, url, token="", body=None):
            calls.append(url)
            page = int(url.split("page=")[1].split("&")[0])
            return pages.get(page, [])

        repo_manager._req = fake_req
        try:
            got = vcs_index._fetch_gitcode_numbers("o", "r", "t")
        finally:
            repo_manager._req = original
        assert got == {1, 2, 3}
        assert len(calls) == 3

    def test_fetch_github_short_page_not_treated_as_end(self):
        import src.user.repo_manager as repo_manager
        pages = {
            1: [{"number": 1}, {"number": 2}],
            2: [{"number": 3}, {"number": 9, "pull_request": {"url": "x"}}],
            3: [],
        }
        calls = []
        original = repo_manager._github_req

        def fake_req(method, url, token="", body=None):
            calls.append(url)
            page = int(url.split("page=")[1].split("&")[0])
            return pages.get(page, [])

        repo_manager._github_req = fake_req
        try:
            got = vcs_index._fetch_github_numbers("o", "r", "t")
        finally:
            repo_manager._github_req = original
        assert got == {1, 2, 3}
        assert len(calls) == 3

    def test_many_unresolved_candidates_fail_open_without_bulk_delete(self):
        """候选 > _CONFIRM_LIMIT 时整体 unknown，绝不自动确认删除。"""
        for n in range(1, 102):
            self._mk(n)
        unresolved = vcs_index._apply_existence(self.P_REPO_KEY, frozenset({9999}))
        assert unresolved == frozenset(range(1, 102))
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM issue WHERE repo_key=? AND vcs_exists=0",
                      (self.P_REPO_KEY,))
            assert c.fetchone()[0] == 0

    def test_apply_write_failure_maps_unavailable_and_fail_open(self):
        """回写失败必须 fail-open（status=unavailable），卡片不隐藏。"""
        self._mk(50)
        self._mk(51)
        original = self._patch_numbers({self.P_REPO_KEY: frozenset({50})})
        original_apply = vcs_index._apply_existence
        vcs_index._apply_existence = lambda rk, nums: None
        try:
            d = self._get("?mode=time&per_page=50").json()
            assert {it["issue_number"] for it in d["issues"]} == {50, 51}
            assert all(it["vcs_check"] == "unavailable" for it in d["issues"])
        finally:
            vcs_index._apply_existence = original_apply
            self._restore(original)

    def test_single_card_unknown_reports_unavailable_not_ok(self):
        """provider ok 但单卡复核 unknown 时，该卡片 vcs_check=unavailable 且不隐藏。"""
        self._mk(60)
        self._mk(61)
        original = self._patch_numbers(
            {self.P_REPO_KEY: frozenset({60})}, confirm_state="unknown")
        try:
            d = self._get("?mode=time&per_page=50").json()
            by_num = {it["issue_number"]: it for it in d["issues"]}
            assert by_num[60]["vcs_check"] == "ok"
            assert by_num[61]["vcs_check"] == "unavailable"
            assert by_num[61]["vcs_exists"] is True
            assert by_num[61]["is_vcs_deleted"] is False
        finally:
            self._restore(original)
