"""Issue #1816：PR 订阅卡片/记录参与全局排序。

覆盖：
- 需求中心（GET /api/issues/{o}/{r}，per_page=0 线上全量路径）
  * mode=time desc：PR 伪卡片按 created_at 与真实 Issue 混排，最新在最前
  * mode=time asc：整体反转，PR 位置同步
  * mode=default：状态分组正确、组内按时间，PR 与同状态 Issue 混排
  * per_page>0：分页下 total 含 PR 卡、逐页无重复无丢失（PR 卡并入统一
    LIMIT/OFFSET 窗口，可跨页出现；本用例的 PR 卡恰为最新一条，故落在第 1 页）
- 流水线页（GET /api/pipeline/{o}/{r}）：PR#N 记录按 raw_issue_id 的真实最大 seq
  参与倒序混排（场景过滤后的场景详情页数据同源自动一致）。
"""
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

client = TestClient(app)


class TestIssue1816PrCardOrdering:
    P_OWNER = "test1816i"
    P_REPO = "ordering"
    P_REPO_KEY = f"gitcode:{P_OWNER}/{P_REPO}"
    PIPE_OWNER = "test1816p"
    PIPE_REPO = "pipeline"
    PIPE_REPO_KEY = f"gitcode:{PIPE_OWNER}/{PIPE_REPO}"
    SCENE = "scene-1816"

    @classmethod
    def setup_class(cls):
        cls._uid = grape_account_db.create_grape_account("test1816_user", email=None, password="x")
        if not cls._uid:
            cls._uid = grape_account_db.get_grape_account_by_username("test1816_user")
        cls._headers = {"Authorization": "Bearer " + create_access_token(
            cls._uid["id"], cls._uid["username"], get_token_version(cls._uid["id"]))}
        try:
            from src.api import db_setup
            db_setup.init_database()
        except Exception:
            pass
        with db_lock:
            c = db.cursor()
            for rk, owner, name in (
                (cls.P_REPO_KEY, cls.P_OWNER, cls.P_REPO),
                (cls.PIPE_REPO_KEY, cls.PIPE_OWNER, cls.PIPE_REPO),
            ):
                c.execute(
                    "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES (?,?,?,?)", (rk, owner, name, f"{owner}/{name}"))
                c.execute(
                    "INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) "
                    "VALUES (?,?,1,1)", (cls._uid["id"], rk))
            db.commit()

    def _clean(self):
        with db_lock:
            c = db.cursor()
            for rk in (self.P_REPO_KEY, self.PIPE_REPO_KEY):
                c.execute("DELETE FROM issue WHERE repo_key=?", (rk,))
                c.execute("DELETE FROM events WHERE repo_key=?", (rk,))
            db.commit()

    def setup_method(self):
        self._clean()

    def teardown_method(self):
        self._clean()

    def _mk_issue(self, number, status, created_at, title="Issue #{n}"):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self._uid["id"], "grape_bot", self.P_REPO_KEY, number,
                 title.format(n=number), status, created_at))
            db.commit()

    def _mk_pr_event(self, pr_num, status, created_at, title=None):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.P_REPO_KEY, f"#pr_{pr_num}_{self.SCENE}", "note_hook_pr", "qa",
                 title or f"PR {pr_num}", f"Sum pr{pr_num}", status, created_at))
            db.commit()

    def _mk_pipe_event(self, issue_id, status="completed"):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status, scene_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.PIPE_REPO_KEY, issue_id, "note_hook_pr", "qa",
                 f"event {issue_id}", "summary", status, self.SCENE))
            db.commit()

    def _get_issues(self, qs=""):
        return client.get(f"/api/issues/{self.P_OWNER}/{self.P_REPO}{qs}",
                          headers=self._headers)

    def _get_pipeline(self, qs=""):
        return client.get(f"/api/pipeline/{self.PIPE_OWNER}/{self.PIPE_REPO}{qs}",
                          headers=self._headers)

    def test_time_desc_merges_pr_cards_into_issues(self):
        self._mk_issue(7101, "processing", "2026-10-01 10:00:00")
        self._mk_issue(7102, "processing", "2026-10-03 10:00:00")
        self._mk_pr_event(70, "completed", "2026-10-02 10:00:00")

        d = self._get_issues("?mode=time&sort_order=desc").json()
        seq = [(it["issue_number"], bool(it.get("is_pr_event"))) for it in d["issues"]]
        assert seq == [(7102, False), (70, True), (7101, False)], seq

    def test_time_asc_reverses_pr_cards(self):
        self._mk_issue(7111, "processing", "2026-10-01 10:00:00")
        self._mk_issue(7112, "processing", "2026-10-03 10:00:00")
        self._mk_pr_event(71, "completed", "2026-10-02 10:00:00")

        d = self._get_issues("?mode=time&sort_order=asc").json()
        seq = [(it["issue_number"], bool(it.get("is_pr_event"))) for it in d["issues"]]
        assert seq == [(7111, False), (71, True), (7112, False)], seq

    def test_default_mode_groups_by_status_and_mixes_pr(self):
        self._mk_issue(7201, "active", "2026-10-05 10:00:00")
        self._mk_issue(7202, "processing", "2026-10-04 00:00:00")
        self._mk_issue(7203, "completed", "2026-10-06 00:00:00")
        self._mk_pr_event(72, "running", "2026-10-04 12:00:00")     # → processing
        self._mk_pr_event(73, "completed", "2026-10-06 12:00:00")   # → completed

        d = self._get_issues("?mode=default&sort_order=desc").json()
        seq = [it["issue_number"] for it in d["issues"]]
        # active → processing（组内时间倒序，PR 72 在 7202 前）→ completed（PR 73 在 7203 前）
        assert seq == [7201, 72, 7202, 73, 7203], seq
        assert len([it for it in d["issues"] if it.get("is_pr_event")]) == 2

    def test_paged_total_includes_pr_cards_no_duplicate_no_loss(self):
        self._mk_issue(7301, "processing", "2026-10-01 10:00:00")
        self._mk_issue(7302, "processing", "2026-10-02 10:00:00")
        self._mk_issue(7303, "processing", "2026-10-03 10:00:00")
        self._mk_pr_event(74, "completed", "2026-10-04 10:00:00")

        d1 = self._get_issues("?mode=time&sort_order=desc&per_page=2&page=1").json()
        assert d1["total"] == 4
        assert d1["has_more"] is True
        # 本用例中 PR 卡恰为最新一条，落在第 1 页；分页契约本身允许 PR 卡跨页出现
        assert any(it.get("is_pr_event") for it in d1["issues"]), "本用例 PR 卡应出现在第 1 页"

        seen = []
        pg = 1
        while True:
            d = self._get_issues(f"?mode=time&sort_order=desc&per_page=2&page={pg}").json()
            seen.extend([it["issue_number"] for it in d["issues"]])
            if not d["has_more"]:
                break
            pg += 1
            assert pg < 20
        assert d["total"] == 4
        assert len(seen) == 4 and len(set(seen)) == 4

    def test_pipeline_pr_rows_sorted_by_raw_max_seq(self):
        # 事件 seq 由插入顺序决定：PR80 最早 → 真实 Issue 中间 → PR81 最新
        self._mk_pipe_event("#pr_80_scene-1816")
        self._mk_pipe_event("#8101")
        self._mk_pipe_event("#pr_81_scene-1816")

        d = self._get_pipeline("?history_limit=10").json()
        ordered = [d["active"]["issue_id"]] + [it["issue_id"] for it in d["history"]]
        assert ordered == ["PR#81", "#8101", "PR#80"], ordered

    def test_pipeline_scene_filter_same_order(self):
        self._mk_pipe_event("#pr_82_scene-1816")
        self._mk_pipe_event("#8102")
        self._mk_pipe_event("#pr_83_scene-1816")

        d = self._get_pipeline(f"?scene_id={self.SCENE}&history_limit=10").json()
        ordered = [d["active"]["issue_id"]] + [it["issue_id"] for it in d["history"]]
        assert ordered == ["PR#83", "#8102", "PR#82"], ordered
