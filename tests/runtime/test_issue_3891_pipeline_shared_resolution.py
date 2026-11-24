"""Issue #3891：`/pipeline` 与主资源共用 issue 解析链路（外部托管仓既有 issue 不再 404）。

生产现象（Issue 正文实测）：外部托管仓 `songspace/flask-blog` 编号 42/119–125 的
`GET /api/issues/{owner}/{repo}/{number}` 返回 200（`issue_id=null`），而
`GET .../{number}/pipeline` 稳定 404「Issue #N 不存在」，端点自相矛盾、客户端轮询不自愈。

本文件的 seam = HTTP 路由层（`TestClient`），与先例 `test_issue_672_dual_path_resolution.py`
与 `test_issues_api.py::TestIssuePipeline` 同层；VCS 外呼在既有 seam
（`_vcs_issue_lookup` / `fetch_issue_via_vcs`）处 patch，不外呼真实平台。
"""
import os
import sys
import uuid
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.runtime.routes import issues as issues_mod
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

OWNER = "org"

REPO = "hosted"

REPO_KEY = f"gitcode:{OWNER}/{REPO}"

# Issue 正文复现编号：42 + 119..125
ISSUE_NUMBERS = (42, 119, 120, 121, 122, 123, 124, 125)

_uid = None


def _session_headers(repo_key=REPO_KEY, agent_type="coding"):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i3891-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i3891_user'")
        db.commit()
    from src.domains.mgmt.auth import grape_account_db
    # 测试账户口令用随机值（不写字面量）：避免开源增量敏感扫描把假口令误报为凭据
    _uid = grape_account_db.create_grape_account(
        "i3891_user", email=None, password=uuid.uuid4().hex)
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("i3891_user")
    _cleanup()


def teardown_module():
    _cleanup()
    if _uid:
        from src.domains.mgmt.auth import grape_account_db
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM grape_accounts WHERE username='i3891_user'")
        db.commit()


def _cleanup():
    with db_lock:
        c = db.cursor()
        for table in ("events", "issue", "issue_state", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _mk_registered(number, stage="qa", with_event=True):
    """在平台内部登记一条 issue（issue 单表权威路径），可选带一条事件。"""
    from src.api.session_store import set_issue_stage
    set_issue_stage(REPO_KEY, f"#{number}", stage, stage, assignee="grape_bot")
    if with_event:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (REPO_KEY, f"#{number}", "issue_opened", "vod", f"Title #{number}", "s", "completed"))
            db.commit()


def _hosted_issue(number):
    """外部托管仓既有 issue 的 VCS 原文形状（内部无行、issue_id=null）。"""
    return {"provider": "gitcode",
            "issue": {"id": 4491800 + int(number), "number": int(number),
                      "title": f"外部托管仓既有需求 #{number}", "state": "open"}}


def _vcs_lookup_by_number(owner, repo, number, user):
    return _hosted_issue(number)


def _internal_row_count(number):
    with db_lock:
        row = db.cursor().execute(
            "SELECT COUNT(*) FROM issue WHERE repo_key=? AND issue_number=?",
            (REPO_KEY, int(number))).fetchone()
    return int(row[0] or 0)


class TestPipelineVcsFallback:
    def test_pipeline_vcs_fallback_returns_empty_pipeline(self):
        """外部托管仓既有 issue：/pipeline 返回 200 空 pipeline + 可判别诊断字段。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", side_effect=_vcs_lookup_by_number):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/124/pipeline", headers=_session_headers())
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["status"] == "unknown"
        assert p["segments"] == []
        assert p["issue"]["number"] == 124
        assert p["diagnostics"] == {
            "exists": True,
            "has_pipeline_data": False,
            "reason": "issue_unregistered",
            "source": "vcs",
        }

    def test_eight_numbers_two_endpoints_agree(self):
        """编号 42/119–125：主资源与 /pipeline 对同一编号的存在性/状态口径一致。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", side_effect=_vcs_lookup_by_number):
            for n in ISSUE_NUMBERS:
                d = client.get(f"/api/issues/{OWNER}/{REPO}/{n}", headers=_session_headers())
                p = client.get(f"/api/issues/{OWNER}/{REPO}/{n}/pipeline", headers=_session_headers())
                assert d.status_code == 200, (n, d.text)
                assert p.status_code == 200, (n, p.text)
                _d, _p = d.json(), p.json()
                assert _d["source"] == "vcs" and _d["issue_id"] is None
                assert _d["pipeline"]["status"] == _p["status"] == "unknown"
                assert _d["pipeline"]["segments"] == _p["segments"] == []
                assert _p["diagnostics"]["exists"] is True
                assert _p["diagnostics"]["has_pipeline_data"] is False
                assert _p["issue"]["number"] == _d["number"] == n


class TestMissingSemantics:
    def test_truly_missing_404_is_discriminable(self):
        """VCS 侧明确无此 issue：两端一致 404，且 reason/error_code 可与「无 pipeline 数据」区分。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
             patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
                   return_value=("missing", {})):
            d = client.get(f"/api/issues/{OWNER}/{REPO}/124", headers=_session_headers())
            p = client.get(f"/api/issues/{OWNER}/{REPO}/124/pipeline", headers=_session_headers())
        assert d.status_code == 404 and p.status_code == 404
        _dd, _pd = d.json()["detail"], p.json()["detail"]
        assert _dd["code"] == 404 and _pd["code"] == 404
        assert _dd["reason"] == "issue_not_found"
        assert _pd["reason"] == "issue_not_found"
        assert _dd["error_code"] == "issue_not_found"
        assert _pd["error_code"] == "issue_not_found"

    def test_unverifiable_vcs_404_internal_sync_missing(self):
        """VCS 无法核实（无 token/异常）：两端一致 404 internal_sync_missing，不伪装成 200 空段。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
             patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
                   return_value=("unknown", {})):
            d = client.get(f"/api/issues/{OWNER}/{REPO}/124", headers=_session_headers())
            p = client.get(f"/api/issues/{OWNER}/{REPO}/124/pipeline", headers=_session_headers())
        assert d.status_code == 404 and p.status_code == 404
        assert d.json()["detail"]["reason"] == "internal_sync_missing"
        assert p.json()["detail"]["reason"] == "internal_sync_missing"


class TestRegisteredIssueUnchanged:
    def test_registered_issue_pipeline_shape_unchanged(self):
        """回归：已登记 issue 的 /pipeline 结构与行为不变（有 segments、无 diagnostics 增量）。"""
        _cleanup()
        _mk_registered(42, stage="qa", with_event=True)
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None) as _lookup:
            r = client.get(f"/api/issues/{OWNER}/{REPO}/42/pipeline", headers=_session_headers())
        assert r.status_code == 200, r.text
        p = r.json()
        assert len(p["segments"]) >= 1
        assert "diagnostics" not in p
        assert "source" not in p
        _lookup.assert_not_called()  # 已登记 issue 不触发 VCS 外呼

    def test_registered_issue_without_events_unchanged(self):
        """回归：已登记但无事件/档案的 issue 维持既有 200 空 pipeline（结构与基线一致）。"""
        _cleanup()
        _mk_registered(119, stage="vod", with_event=False)
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/119/pipeline", headers=_session_headers())
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["segments"] == [] and p["status"] == "unknown"
        assert "diagnostics" not in p


class TestPipelineReadOnly:
    def test_pipeline_does_not_backfill_internal_row(self):
        """/pipeline 只读解析：VCS 核实存在也不补建内部行（补建仅发生在主资源 detail 路径）。"""
        _cleanup()
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
             patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
                   return_value=("exists", {"title": "外部托管仓既有需求"})), \
             patch("src.domains.runtime.issues_center.ensure_issue_task",
                   side_effect=AssertionError("pipeline 不得补建内部行")):
            r = client.get(f"/api/issues/{OWNER}/{REPO}/125/pipeline", headers=_session_headers())
        assert r.status_code == 200, r.text
        assert r.json()["diagnostics"]["exists"] is True
        assert _internal_row_count(125) == 0