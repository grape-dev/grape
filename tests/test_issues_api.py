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