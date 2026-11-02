"""Issue #672：pending-review / issues detail 双路径解析回归测试。

复现 eduSwarm 仓三组失败模式：
1. 内部主键 miss（GRAPE_ISSUE_ID 注入 404）→ 按 GitCode number 兜底解析；
2. --issue_id 撞上其它仓库内部 id → 403 作用域误判 → number 兜底修正；
3. issues detail 本地聚合 miss 但 git-repo issues get 正常 → VCS 兜底，同编号 200 一致。
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import issues as issues_mod
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.auth import grape_account_db

client = TestClient(app)

_uid = None


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


def _session_headers(repo_key="gitcode:org/test", agent_type="qa"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i672-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i672_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='i672_eric'")
        db.commit()
    _uid = grape_account_db.create_grape_account("i672_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("i672_user")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (_uid["id"], "i672_eric", "ou_i672", "Eric672"))
        db.commit()
    user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test", True, True)


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i672_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='i672_eric'")
        db.commit()


def _cleanup(numbers=(1089,)):
    with db_lock:
        c = db.cursor()
        for n in numbers:
            c.execute("DELETE FROM issue WHERE issue_number=? AND repo_key IN ('gitcode:org/test','gitcode:org/other')", (n,))
        db.commit()


def _seed_number_row(number=1089, status="processing", repo="gitcode:org/test"):
    """在会话仓库作用域内按 issue_number 建行（模拟 eduSwarm 编号卡片）。返回内部主键。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, stage) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_uid["id"], "i672_eric", repo, number, "eduSwarm 需求", "d", status, "qa"))
        cid = c.lastrowid
        db.commit()
        return cid


class TestPendingReviewDualPath:
    def test_internal_id_miss_resolved_by_number(self):
        """失败模式 1：注入内部 id 在表内无主键行，但同值是会话仓库的 GitCode number → 兜底命中。"""
        _cleanup()
        cid = _seed_number_row()
        assert cid != 1089 or True  # 主键由自增分配，与 number 无必然相等
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/issues/1089/pending-review",
                            headers=_session_headers(agent_type="qa"),
                            json={"review_points": "请确认是否合入该 PR"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["changed"] is True
        assert data["issue_id"] == cid
        with db_lock:
            st = db.cursor().execute(
                "SELECT status FROM issue WHERE id=?", (cid,)).fetchone()
        assert st[0] == "pending_review"

    def test_id_collision_other_repo_resolved_by_number(self):
        """失败模式 2：issue_id=1089 撞上其它仓库的内部主键（旧 403 作用域误判）→ number 兜底修正。"""
        _cleanup()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE id=1089")
            c.execute(
                "INSERT INTO issue (id, user_id, assignee, repo_key, issue_number, title, status, stage) "
                "VALUES (1089, 0, '', 'gitcode:org/other', 555, '别的仓', 'processing', 'vod')")
            db.commit()
        try:
            cid = _seed_number_row()
            with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post("/api/issues/1089/pending-review",
                                headers=_session_headers(agent_type="qa"),
                                json={"review_points": "审核点"})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["issue_id"] == cid
            assert data["changed"] is True
            # 其它仓库的 1089 行不被误改
            with db_lock:
                other = db.cursor().execute(
                    "SELECT status FROM issue WHERE id=1089").fetchone()
            assert other[0] == "processing"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM issue WHERE id=1089")
                db.commit()

    def test_vcs_fallback_creates_missing_row(self):
        """失败模式 1 强化：本地完全无行（eduSwarm 存量缺行），远端 number 存在 → 建最小行并置审核点。"""
        _cleanup()
        _fake = {"provider": "gitcode", "issue": {"id": 4491872, "number": 1089, "title": "eduSwarm 远端需求", "state": "open"}}
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=_fake):
            with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post("/api/issues/1089/pending-review",
                                headers=_session_headers(agent_type="qa"),
                                json={"review_points": "请确认"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["changed"] is True
        with db_lock:
            row = db.cursor().execute(
                "SELECT id, status, title FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=1089").fetchone()
        assert row and row[1] == "pending_review"
        assert row[2] == "eduSwarm 远端需求"

    def test_totally_unknown_issue_404(self):
        """守卫：内部 id 与 number 双路径 + 远端均未命中 → 维持 404。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None):
            r = client.post("/api/issues/999999/pending-review",
                            headers=_session_headers(agent_type="qa"),
                            json={"review_points": "x"})
        assert r.status_code == 404

    def test_existing_internal_id_still_works(self):
        """回归：内部主键路径原语义不变。"""
        _cleanup()
        cid = _seed_number_row()
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(agent_type="qa"),
                            json={"review_points": "审核点"})
        assert r.status_code == 200 and r.json()["issue_id"] == cid

    def test_cross_repo_number_not_resolved_without_local_row(self):
        """守卫：会话仓库作用域内无该 number 且远端未确认 → 不允许借道其它仓库的内部行（403 保持）。"""
        _cleanup()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE id=1089")
            c.execute(
                "INSERT INTO issue (id, user_id, assignee, repo_key, issue_number, title, status, stage) "
                "VALUES (1089, 0, '', 'gitcode:org/other', 555, '别的仓', 'processing', 'vod')")
            db.commit()
        try:
            with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None):
                r = client.post("/api/issues/1089/pending-review",
                                headers=_session_headers(agent_type="qa"),
                                json={"review_points": "x"})
            assert r.status_code == 403
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM issue WHERE id=1089")
                db.commit()


class TestIssueDetailVcsFallback:
    def test_detail_local_miss_vcs_hit(self):
        """失败模式 3：detail 本地聚合 miss，git-repo 通道同编号存在 → 200 且字段与 VCS 一致。"""
        _cleanup()
        _fake = {"provider": "gitcode", "issue": {"id": 4491872, "number": 1089,
                                                  "title": "eduSwarm 远端需求", "state": "open"}}
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=_fake):
            r = client.get("/api/issues/org/test/1089", headers=_session_headers(agent_type="qa"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["source"] == "vcs"
        assert data["number"] == 1089
        assert data["title"] == "eduSwarm 远端需求"
        assert data["vcs_id"] == 4491872
        assert data["events"] == [] and data["pipeline"]["segments"] == []

    def test_detail_local_miss_vcs_miss_404(self):
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None):
            r = client.get("/api/issues/org/test/1089", headers=_session_headers(agent_type="qa"))
        assert r.status_code == 404

    def test_detail_local_hit_no_vcs_call(self):
        """回归：本地已聚合的编号不触发 VCS 兜底（原路径返回，无 source 字段）。"""
        _cleanup()
        cid = _seed_number_row()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None) as _m:
            r = client.get("/api/issues/org/test/1089", headers=_session_headers(agent_type="qa"))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "source" not in data
        assert data["number"] == 1089
