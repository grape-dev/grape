"""Issues 对外接口测试（/api/issues）。

覆盖：仓库 Issue 列表（stage/status 聚合）、创建 Issue、详情（stage+events+pipeline）、
事件列表、pipeline 路线图；认证、聚合隔离、404 兜底。
"""
import os, json, io, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.auth import grape_account_db
from src.session_manager import db, db_lock

def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))

client = TestClient(app)

_uid = None
_headers = {}

OWNER = "test_issues"
REPO = "hello"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_issues", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_issues")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    # 确保基础表已建（issue_state/tasks/events）
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass


def teardown_module():
    with db_lock:
        c = db.cursor()
        for t in ("events", "issue_state", "issue", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _mk_issue_state(number, stage="qa", owner="qa", scene="default", assignee="grape_bot"):
    """Phase C3：经 set_issue_stage 写（issue 单表权威；同时建 issue 卡片行）。"""
    from src.api.session_store import set_issue_stage, set_issue_scene
    iid = f"#{number}"
    set_issue_stage(REPO_KEY, iid, stage, owner, assignee=assignee)
    if scene:
        set_issue_scene(REPO_KEY, iid, scene, 0)


def _mk_event(number, ev_type="issue_opened", agent="vod", status="completed"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (REPO_KEY, f"#{number}", ev_type, agent, f"Title #{number}", f"Summary #{number}", status))
        seq = c.lastrowid
        db.commit()
        return seq


def _mk_task(number, status="processing"):
    """确保 issue 卡片行存在并按需更新 status（不清零 stage 等收编字段）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
            (_uid["id"], "grape_bot", REPO_KEY, number, f"Task #{number}", status,
             REPO_KEY, number))
        c.execute(
            "UPDATE issue SET status=? WHERE repo_key=? AND issue_number=?",
            (status, REPO_KEY, number))
        db.commit()


# ===================== 列表 =====================

class TestListIssues:
    def test_empty_repo(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["issues"] == [] and data["total"] == 0

    def test_aggregates_stage_and_status(self):
        _mk_issue_state(5, stage="qa", owner="qa", scene="default")
        _mk_event(5, "issue_opened", "vod")
        _mk_event(5, "pull_request_updated", "qa")
        _mk_task(5, status="processing")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        assert resp.status_code == 200
        issues = resp.json()["issues"]
        assert len(issues) >= 1
        it = next(i for i in issues if i["number"] == 5)
        assert it["stage"] == "qa"
        assert it["task_status"] == "processing"
        assert it["owner_agent"] == "qa"
        assert it["scene_id"] == "default"
        assert it["events_count"] == 2
        assert it["latest_event"]["event_type"] == "pull_request_updated"

    def test_events_only_scoped_by_issue(self):
        _mk_issue_state(6, stage="sre")
        _mk_event(6, "issue_opened", "vod")
        _mk_event(6, "pull_request_merged", "sre")
        _mk_issue_state(7, stage="vod")
        _mk_event(7, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_headers)
        it6 = next(i for i in resp.json()["issues"] if i["number"] == 6)
        it7 = next(i for i in resp.json()["issues"] if i["number"] == 7)
        assert it6["events_count"] == 2
        assert it7["events_count"] == 1

    def test_requires_auth(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}")
        assert resp.status_code == 401


# ===================== 详情 =====================

class TestIssueDetail:
    def test_detail_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99999", headers=_headers)
        assert resp.status_code == 404

    def test_detail_aggregate(self):
        _mk_issue_state(8, stage="coding", owner="coding", scene="iac")
        _mk_event(8, "issue_opened", "vod")
        _mk_event(8, "pull_request_opened", "coding")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/8", headers=_headers)
        assert resp.status_code == 200
        d = resp.json()
        assert d["stage"] == "coding" and d["owner_agent"] == "coding"
        assert d["scene_id"] == "iac"
        assert len(d["events"]) == 2
        assert "pipeline" in d

    def test_detail_no_issue_state_fallback(self):
        _mk_event(9, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/9", headers=_headers)
        assert resp.status_code == 200
        d = resp.json()
        assert d["stage"] == "vod"  # 无 issue_state → 兜底 vod
        assert len(d["events"]) == 1


# ===================== 事件列表 =====================

class TestIssueEvents:
    def test_events_scope(self):
        _mk_issue_state(10, stage="qa")
        _mk_event(10, "issue_opened", "vod")
        _mk_event(10, "pull_request_updated", "qa")
        _mk_issue_state(11, stage="vod")
        _mk_event(11, "issue_opened", "vod")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/10/events", headers=_headers)
        assert resp.status_code == 200
        evs = resp.json()["events"]
        assert len(evs) == 2
        assert all(e["event_type"] in ("issue_opened", "pull_request_updated") for e in evs)

    def test_events_empty(self):
        _mk_issue_state(12, stage="done")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/12/events", headers=_headers)
        assert resp.status_code == 200
        assert resp.json() == {"events": [], "total": 0}

    def test_events_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99998/events", headers=_headers)
        assert resp.status_code == 404


# ===================== Pipeline =====================

class TestIssuePipeline:
    def test_pipeline_without_archive(self):
        _mk_issue_state(13, stage="sre")
        _mk_event(13, "issue_opened", "vod")
        _mk_event(13, "pull_request_merged", "sre")
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/13/pipeline", headers=_headers)
        assert resp.status_code == 200
        p = resp.json()
        assert "segments" in p
        assert len(p["segments"]) >= 1
        assert p["segments"][0]["seq"] >= 1

    def test_pipeline_not_found(self):
        resp = client.get(f"/api/issues/{OWNER}/{REPO}/99997/pipeline", headers=_headers)
        assert resp.status_code == 404


# ===================== 创建 =====================

class TestCreateIssue:
    def test_create_missing_fields(self):
        resp = client.post(f"/api/issues/{OWNER}/{REPO}", json={"title": ""}, headers=_headers)
        assert resp.status_code == 400

    def test_create_ok(self):
        from unittest.mock import patch
        _mk_issue_state(20, stage="vod")  # 预置避免走 GitCode API
        # Issue #343 改为：用户建单用用户自己的 GitCode token + _post_gitcode_issue
        with patch("src.api.issues_center._find_open_issue_by_title", return_value=None),              patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"),              patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 20}),              patch("src.api.issues_center.ensure_issue_task") as _mock_ensure:
            _mock_ensure.return_value = 20
            resp = client.post(
                f"/api/issues/{OWNER}/{REPO}",
                json={"title": "新需求", "description": "描述", "repo_key": REPO_KEY},
                headers=_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("issue_number") == 20
        assert data.get("created") is True

    def test_create_duplicate_title_not_blocked(self):
        """Issue #202: 同名需求不再被 409 阻断——即使已存在同名 open Issue，仍允许创建新 Issue。"""
        from unittest.mock import patch
        # Issue #343 改为：用户建单用用户自己的 GitCode token + _post_gitcode_issue
        # 幂等查重 mock 为「无同名 open Issue」，避免打到真实 GitCode API
        with patch("src.api.deps.get_user_token_with_refresh", return_value="test_tok"),              patch("src.api.issues_center._find_open_issue_by_title", return_value=None),              patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 21}),              patch("src.api.issues_center.ensure_issue_task") as _mock_ensure:
            _mock_ensure.return_value = 21
            resp = client.post(
                f"/api/issues/{OWNER}/{REPO}",
                json={"title": "重复标题", "description": "描述", "repo_key": REPO_KEY},
                headers=_headers)
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        assert data.get("issue_number") == 21
        assert data.get("created") is True

    # ====== Issue #345: 建单 401 走 reauth_required 协议（按 token 归属区分）======
    # 前端 api.js handleResp 收到 HTTP 401 + detail.action='reauth_required' 会自动跳 GitCode OAuth 授权页，
    # 强制用户重新授权（用户自己的 GitCode 凭据失效自愈）；bot/github 路径保持 502「检查仓库 Bot Token 配置」。

    def test_create_dedup_401_user_token_refresh_fail_reauth(self):
        """用户 token 查重 401 且刷新失败 → 返回 401 + reauth_required，引导重新授权 GitCode
        （生产事故：hello-calculator-11 建单失败，但当时 502 纯文案不触发前端强制跳授权页）。"""
        from unittest.mock import patch
        def _raise_401(*a, **k):
            raise RuntimeError("GitCode API GET https://gitcode.com/api/v5/repos/o/r/issues?state=open failed (401): UNAUTHORIZED")
        with patch("src.api.deps.get_user_token_with_refresh", return_value="dead_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", side_effect=_raise_401), \
             patch("src.api.issues_center._refresh_user_gitcode_token", return_value=""):
            resp = client.post(f"/api/issues/{OWNER}/{REPO}",
                               json={"title": "t_401", "description": "d", "repo_key": REPO_KEY},
                               headers=_headers)
        assert resp.status_code == 401, resp.text
        detail = resp.json()["detail"]
        assert detail.get("action") == "reauth_required", detail
        assert "登录已失效" in detail["message"], detail
        assert "重新授权" in detail["message"] or "重新绑定" in detail["message"], detail
        assert "Bot Token" not in detail["message"], detail

    def test_create_dedup_401_user_token_refresh_ok_proceeds(self):
        """用户 token 401 → 刷新成功并用新 token 重试查重 → 正常创建，不跳授权。"""
        from unittest.mock import patch
        calls = {"n": 0}
        def _dedup(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("GitCode API GET ... failed (401): UNAUTHORIZED")
            return None  # 刷新后重试成功：无同名 open issue
        with patch("src.api.deps.get_user_token_with_refresh", return_value="dead_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", side_effect=_dedup), \
             patch("src.api.issues_center._refresh_user_gitcode_token", return_value="new_tok"), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 23}), \
             patch("src.api.issues_center.ensure_issue_task", return_value=23):
            resp = client.post(f"/api/issues/{OWNER}/{REPO}",
                               json={"title": "t_401b", "description": "d", "repo_key": REPO_KEY},
                               headers=_headers)
        assert resp.status_code == 200, resp.text
        assert calls["n"] == 2
        assert resp.json()["issue_number"] == 23

    def test_create_dedup_401_user_token_retry_still_fails_reauth(self):
        """刷新成功但重试查重仍 401 → refresh_token 也失效 → 401 + reauth_required。"""
        from unittest.mock import patch
        def _raise_401(*a, **k):
            raise RuntimeError("GitCode API GET ... failed (401): UNAUTHORIZED")
        with patch("src.api.deps.get_user_token_with_refresh", return_value="dead_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", side_effect=_raise_401), \
             patch("src.api.issues_center._refresh_user_gitcode_token", return_value="new_tok"):
            resp = client.post(f"/api/issues/{OWNER}/{REPO}",
                               json={"title": "t_401c", "description": "d", "repo_key": REPO_KEY},
                               headers=_headers)
        assert resp.status_code == 401, resp.text
        detail = resp.json()["detail"]
        assert detail.get("action") == "reauth_required", detail
        assert "登录已失效" in detail["message"], detail

    def test_create_post_401_user_token_reauth(self):
        """建 Issue POST 401（刷新失败）→ 401 + reauth_required，前端强制跳 GitCode 授权页。"""
        from unittest.mock import patch
        def _raise_401(*a, **k):
            raise RuntimeError("GitCode API POST issues failed (401): UNAUTHORIZED")
        with patch("src.api.deps.get_user_token_with_refresh", return_value="dead_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue", side_effect=_raise_401), \
             patch("src.api.issues_center._refresh_user_gitcode_token", return_value=""):
            resp = client.post(f"/api/issues/{OWNER}/{REPO}",
                               json={"title": "t_post401", "description": "d", "repo_key": REPO_KEY},
                               headers=_headers)
        assert resp.status_code == 401, resp.text
        detail = resp.json()["detail"]
        assert detail.get("action") == "reauth_required", detail
        assert "登录已失效" in detail["message"], detail
        assert "Bot Token" not in detail["message"], detail

    def test_create_post_401_user_token_refresh_ok_proceeds(self):
        """建 Issue POST 401 → 刷新成功重试 POST 成功 → 正常建单，不跳授权。"""
        from unittest.mock import patch
        calls = {"post": 0}
        def _post(*a, **k):
            calls["post"] += 1
            if calls["post"] == 1:
                raise RuntimeError("GitCode API POST issues failed (401): UNAUTHORIZED")
            return {"number": 24}
        with patch("src.api.deps.get_user_token_with_refresh", return_value="dead_tok"), \
             patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue", side_effect=_post), \
             patch("src.api.issues_center._refresh_user_gitcode_token", return_value="new_tok"), \
             patch("src.api.issues_center.ensure_issue_task", return_value=24):
            resp = client.post(f"/api/issues/{OWNER}/{REPO}",
                               json={"title": "t_post401b", "description": "d", "repo_key": REPO_KEY},
                               headers=_headers)
        assert resp.status_code == 200, resp.text
        assert calls["post"] == 2
        assert resp.json()["issue_number"] == 24

    def test_create_dedup_401_github_bot_token_message(self):
        """GitHub 建单走仓库 bot token → 401 仍返回 502 提示检查「仓库 Bot Token 配置」，不带 reauth_required
        （bot token 失效不需要用户重新授权 GitCode）。"""
        from unittest.mock import patch
        def _raise_401(*a, **k):
            raise RuntimeError("GitHub API GET ... failed (401): unauthorized")
        with patch("src.user.db.get_github_token", return_value="gh_bot_tok"), \
             patch("src.user.repo_manager._github_req", side_effect=_raise_401):
            resp = client.post("/api/issues/gh_owner/gh_repo",
                               json={"title": "t_gh", "description": "d",
                                     "repo_key": "github:gh_owner/gh_repo"},
                               headers=_headers)
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert "Bot Token" in detail["message"], detail
        assert "action" not in detail, detail

# ===================== Issue #542：真分页 + 终态截断 =====================

class TestListIssuesPaging:
    """Issue #542：/api/issues/{o}/{r} 真分页（page/per_page）与默认终态截断（recent_terminal）。

    用独立仓库（test542i/paging）+ 独立 issue 编号段（5000+），逐用例清理，
    与本文件其他用例（共用 test_issues/hello）完全隔离，支持精确计数断言。
    """

    P_OWNER = "test542i"
    P_REPO = "paging"
    P_REPO_KEY = f"gitcode:{P_OWNER}/{P_REPO}"

    def _mk_many(self, start, count, status):
        """批量建 issue 卡片行（不建 events，列表接口不依赖事件存在）。

        created_at 随 issue_number 单调递增（mode=time 排序下编号序=时间序）。
        """
        from datetime import datetime, timedelta
        _base = datetime(2026, 10, 1)
        with db_lock:
            c = db.cursor()
            for i in range(start, start + count):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_at) "
                    "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
                    (_uid["id"], "grape_bot", self.P_REPO_KEY, i, f"Task #{i}", status,
                     (_base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                     self.P_REPO_KEY, i))
            db.commit()

    def _get(self, qs=""):
        return client.get(f"/api/issues/{self.P_OWNER}/{self.P_REPO}{qs}", headers=_headers)

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (self.P_REPO_KEY,))
            c.execute("DELETE FROM events WHERE repo_key=?", (self.P_REPO_KEY,))
            db.commit()

    def setup_method(self):
        self._clean()

    def teardown_method(self):
        self._clean()

    def test_default_truncates_terminal_to_recent_30(self):
        # 2 条非终态 + 35 条终态 → 默认返回 2 + 30 = 32 条
        self._mk_many(5000, 2, "processing")
        self._mk_many(5100, 35, "completed")
        resp = self._get("?mode=time")
        assert resp.status_code == 200
        data = resp.json()
        issues = data["issues"]
        assert len(issues) == 32, f"expected 32 (2 non-terminal + 30 recent terminal), got {len(issues)}"
        # 全部非终态在列
        nums = {i["issue_number"] for i in issues}
        assert 5000 in nums and 5001 in nums
        # 终态只保留最近 30 条（5100..5134 共 35 条，mode=time&desc → 保留编号最大的 30 条）
        terminal = [n for n in nums if n >= 5100]
        assert len(terminal) == 30
        assert 5100 not in terminal and 5101 not in terminal  # 最旧的 5 条被截断
        assert 5134 in terminal

    def test_default_mode_ordering_with_truncation(self):
        """mode=default 截断后仍保持状态优先序（非终态在前、终态按时间在后）。"""
        self._mk_many(5700, 2, "processing")       # 非终态（优先级 1）
        self._mk_many(5710, 3, "active")           # 非终态（优先级 0）
        self._mk_many(5720, 35, "completed")       # 终态（优先级 4，截最近 30）
        resp = self._get("")                       # 默认 mode=default
        assert resp.status_code == 200
        issues = resp.json()["issues"]
        # 全量 5 条非终态 + 30 条终态
        assert len(issues) == 35, f"expected 35, got {len(issues)}"
        statuses = [i["status"] for i in issues]
        # 非终态全在前 5 位（active 2 条 + processing 3 条），终态在后
        assert set(statuses[:5]) == {"active", "processing"}
        assert all(s == "completed" for s in statuses[5:])
        # active 组内 created_at 降序（编号大在前）
        actives = [i["issue_number"] for i in issues if i["status"] == "active"]
        assert actives == sorted(actives, reverse=True)

    def test_recent_terminal_zero_returns_all(self):
        self._mk_many(5200, 35, "completed")
        resp = self._get("?mode=time&recent_terminal=0")
        assert resp.status_code == 200
        assert len(resp.json()["issues"]) == 35

    def test_explicit_terminal_status_bypasses_truncation(self):
        self._mk_many(5300, 35, "completed")
        resp = self._get("?status=completed")
        assert resp.status_code == 200
        assert len(resp.json()["issues"]) == 35

    def test_per_page_true_pagination(self):
        self._mk_many(5400, 10, "completed")
        resp = self._get("?mode=time&per_page=3&page=1")
        assert resp.status_code == 200
        d1 = resp.json()
        assert d1["total"] == 10
        assert d1["page"] == 1 and d1["per_page"] == 3
        assert len(d1["issues"]) == 3
        assert d1["has_more"] is True
        # 最后一页：offset 9 → 1 条，has_more=False
        resp = self._get("?mode=time&per_page=3&page=4")
        d4 = resp.json()
        assert len(d4["issues"]) == 1
        assert d4["has_more"] is False
        # 逐页编号不重叠且有序（mode=time + desc → 编号降序）
        pages = []
        for pg in (1, 2, 3, 4):
            r = self._get(f"?mode=time&per_page=3&page={pg}")
            pages.extend([i["issue_number"] for i in r.json()["issues"]])
        assert pages == sorted(pages, reverse=True)
        assert len(set(pages)) == 10

    def test_pagination_enrichment_scoped_correctly(self):
        """分页模式下返回卡片的事件聚合字段仍正确（events_count/latest_event 来自本 issue 事件）。"""
        self._mk_many(5500, 5, "processing")
        with db_lock:
            c = db.cursor()
            for i in (5500, 5501, 5502, 5503, 5504):
                c.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (self.P_REPO_KEY, f"#{i}", "issue_opened", "issue", f"Real {i}", f"Sum {i}", "completed"))
            db.commit()
        resp = self._get("?mode=time&per_page=2&page=1")
        d = resp.json()
        assert d["total"] == 5 and len(d["issues"]) == 2
        for it in d["issues"]:
            assert it["events_count"] == 1
            assert it["latest_event"]["title"] == f"Real {it['issue_number']}"

    def test_pagination_pr_cards_only_on_first_page(self):
        """分页模式：PR 订阅伪卡片只混入第 1 页（不逐页重复），has_more 不受 PR 卡片影响。"""
        self._mk_many(5600, 4, "completed")
        with db_lock:
            c = db.cursor()
            # 2 个 PR 订阅伪 ID（各自 1 条事件）
            for pr in (11, 12):
                c.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (self.P_REPO_KEY, f"#pr_{pr}_scene-x", "note_hook_pr", "qa",
                     f"PR #{pr} review", f"Sum pr{pr}", "completed"))
            db.commit()
        d1 = self._get("?mode=time&per_page=2&page=1").json()
        assert d1["total"] == 4  # PR 伪卡片不计入 issue 行 total
        pr_cards = [it for it in d1["issues"] if it.get("is_pr_event")]
        assert len(pr_cards) == 2, "第 1 页应混入 PR 伪卡片"
        # issue 行 2 条 + PR 卡片 2 条
        assert len([it for it in d1["issues"] if not it.get("is_pr_event")]) == 2
        assert d1["has_more"] is True  # 2/4 行 < 4（不被 PR 卡片虚增）
        d2 = self._get("?mode=time&per_page=2&page=2").json()
        assert not [it for it in d2["issues"] if it.get("is_pr_event")], "第 2 页不应重复 PR 伪卡片"
        assert len(d2["issues"]) == 2
        assert d2["has_more"] is False
