"""Issue #539 回归测试：pipeline 需求级业务状态（pending_review）+ SRE 任务/结果引用。

背景（真实样本 #428）：SRE 完成后需求进入 pending_review，而 pipeline 顶层
status=completed 仅表示「事件执行结束」，使用者误认为需求与生产发布已完成。

覆盖场景（Spec 验收标准）：
1. pending_review：pipeline 可识别「待人工确认」，SRE 段含验收结论引用；
2. 验收失败：SRE 任务 failed，task_ref.task_status 显式标注；
3. 取消：review_status=cancelled；
4. 生产已完成：review_status=completed；
5. 生产尚未开始：review_status=in_progress，在途任务 result_ref=pending；
6. 旧字段兼容性：旧字段语义与结构不变，新增字段可选（无任务 → 显式 unavailable）；
7. 脱敏：result_text 摘要不含 token/凭据。
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db
from src.session_manager import db, db_lock

OWNER = "test539"
REPO = "pipe"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_539_pipe", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_539_pipe")
    _headers = {"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"}
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass
    _clean()
    # issues_pipeline_detail（按 issue.id）走 check_repo_access：
    # 需 managed_repos + user_repos 关联（同 test_api_routes 口径）
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id) "
                  "VALUES (?,?,?,?,1)", (REPO_KEY, OWNER, REPO, f"{OWNER}/{REPO}"))
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) VALUES (?,?,1,1)",
                  (_uid["id"], REPO_KEY))
        db.commit()


def teardown_module():
    _teardown_clean()


def _clean():
    """清测试数据行（保留 managed_repos/user_repos 权限行，teardown 再收尾）。"""
    with db_lock:
        c = db.cursor()
        for t in ("events", "issue_state", "issue", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _teardown_clean():
    with db_lock:
        c = db.cursor()
        _rows = ("events", "issue_state", "issue", "agent_tasks")
        for t in _rows:
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        for t in ("managed_repos", "user_repos"):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
        db.commit()


def _seed_issue(number, stage, status):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_uid["id"], "grape_bot", REPO_KEY, number, f"Issue #{number}", status, stage))
        db.commit()


def _mk_event(number, agent, status="completed", ev_type="a2a_event", seq=None):
    with db_lock:
        c = db.cursor()
        if seq:
            c.execute(
                "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, title, summary, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, REPO_KEY, f"#{number}", ev_type, agent, f"Title #{number}",
                 f"A2A: → {agent}", status))
        else:
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (REPO_KEY, f"#{number}", ev_type, agent, f"Title #{number}",
                 f"A2A: → {agent}", status))
            seq = c.lastrowid
        db.commit()
        return seq


def _mk_agent_task(event_seq, agent_type="sre", status="completed", result_text=""):
    task_id = f"tsk_539_{event_seq}_{agent_type}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, result_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, f"sess_{task_id}", 1, event_seq, "", REPO_KEY, agent_type,
             "prompt", status, result_text))
        db.commit()
        return task_id


def _get_pipeline(number):
    return client.get(f"/api/issues/{OWNER}/{REPO}/{number}/pipeline", headers=_headers)


# ===================== 纯函数单测 =====================

class TestDeriveReviewStatus:
    def test_pending_review(self):
        from src.api.pipeline_status import derive_review_status
        assert derive_review_status("pending_review", "pending_review") == "pending_review"
        assert derive_review_status("sre", "pending_review") == "pending_review"
        # Issue #1599：status 为唯一事实源，stage=pending_review 不再单独判待审核
        assert derive_review_status("pending_review", "") == "in_progress"
        assert derive_review_status("pending_review", "processing") == "in_progress"
        assert derive_review_status("pending_review", "awaiting_input") == "awaiting_input"
        assert derive_review_status("pending_review", "completed") == "completed"

    def test_completed(self):
        from src.api.pipeline_status import derive_review_status
        assert derive_review_status("done", "completed") == "completed"
        assert derive_review_status("sre", "completed") == "completed"

    def test_cancelled(self):
        from src.api.pipeline_status import derive_review_status
        assert derive_review_status("sre", "cancelled") == "cancelled"

    def test_awaiting_input(self):
        from src.api.pipeline_status import derive_review_status
        assert derive_review_status("issue", "awaiting_input") == "awaiting_input"

    def test_in_progress_and_unknown(self):
        from src.api.pipeline_status import derive_review_status
        assert derive_review_status("coding", "processing") == "in_progress"
        assert derive_review_status("sre", "active") == "in_progress"
        assert derive_review_status("", "") == "unknown"


class TestResultSummary:
    def test_structured_json_conclusion_preferred(self):
        from src.api.pipeline_status import extract_result_summary
        rt = json.dumps({"conclusion": "验收通过 v1.2.3", "issue_summary": "s",
                         "result_text": "r"}, ensure_ascii=False)
        assert "验收通过 v1.2.3" in extract_result_summary(rt)

    def test_redacts_tokens(self):
        from src.api.pipeline_status import extract_result_summary
        rt = "结论：验收通过，token=tk_abc123def456ghi789 AKLMNOPQRSTUV12"
        out = extract_result_summary(rt)
        assert "tk_abc123def456ghi789" not in out
        assert "AKLMNOPQRSTUV12" not in out
        assert "[REDACTED]" in out

    def test_empty(self):
        from src.api.pipeline_status import extract_result_summary
        assert extract_result_summary("") == ""
        assert extract_result_summary("   ") == ""

    def test_length_limit(self):
        from src.api.pipeline_status import extract_result_summary, _RESULT_SUMMARY_LIMIT
        out = extract_result_summary("x" * 1000)
        assert len(out) <= _RESULT_SUMMARY_LIMIT + 3
        assert out.endswith("...")


# ===================== 场景 1：pending_review（真实样本 #428 复刻） =====================

class TestPendingReviewScenario:
    def test_pending_review_with_sre_result(self):
        _seed_issue(101, stage="pending_review", status="pending_review")
        seqs = {}
        for agent in ("vod", "issue", "coding", "qa", "sre"):
            seqs[agent] = _mk_event(101, agent, status="completed")
        sre_task = _mk_agent_task(
            seqs["sre"], agent_type="sre", status="completed",
            result_text=json.dumps({
                "conclusion": "验收通过：版本 v1.2.3 已部署，冒烟测试 12/12 通过",
                "issue_summary": "SRE 验收通过",
            }, ensure_ascii=False))
        try:
            r = _get_pipeline(101)
            assert r.status_code == 200
            d = r.json()
            # 旧字段语义不变：顶层 status=completed 仅表示事件执行结束
            assert d["status"] == "completed"
            assert d["issue"]["number"] == 101
            assert isinstance(d["segments"], list) and len(d["segments"]) == 5
            # 新增需求级业务状态：可识别「待人工确认」
            assert d["review_status"] == "pending_review"
            assert d["issue_stage"] == "pending_review"
            assert d["issue_task_status"] == "pending_review"
            # SRE 段含任务引用与验收结论引用
            sre_seg = [s for s in d["segments"] if s["agent"] == "sre"][-1]
            tr = sre_seg["output"]["task_ref"]
            assert tr["task_id"] == sre_task
            assert tr["task_status"] == "completed"
            assert tr["query_path"] == f"/api/runtime/tasks/{sre_task}/status"
            rr = sre_seg["output"]["result_ref"]
            assert rr["status"] == "available"
            assert "验收通过" in rr["summary"]
            # 无任务关联的段（vod 等事件型）显式标注 unavailable
            vod_seg = [s for s in d["segments"] if s["agent"] == "vod"][-1]
            assert vod_seg["output"]["task_ref"]["task_id"] is None
            assert vod_seg["output"]["task_ref"]["task_status"] == "unavailable"
            assert vod_seg["output"]["result_ref"]["status"] == "unavailable"
        finally:
            _clean()

    def test_detail_pipeline_nested_enriched(self):
        _seed_issue(102, stage="pending_review", status="pending_review")
        sre_seq = _mk_event(102, "sre", status="completed")
        _mk_agent_task(sre_seq, agent_type="sre", status="completed",
                       result_text=json.dumps({"conclusion": "验收结论 OK"}, ensure_ascii=False))
        try:
            r = client.get(f"/api/issues/{OWNER}/{REPO}/102", headers=_headers)
            assert r.status_code == 200
            p = r.json()["pipeline"]
            assert p["review_status"] == "pending_review"
            sre_seg = [s for s in p["segments"] if s["agent"] == "sre"][-1]
            assert sre_seg["output"]["result_ref"]["status"] == "available"
            assert "验收结论" in sre_seg["output"]["result_ref"]["summary"]
        finally:
            _clean()

    def test_pipeline_detail_by_id_fields(self):
        _seed_issue(103, stage="pending_review", status="pending_review")
        sre_seq = _mk_event(103, "sre", status="completed")
        task_id = _mk_agent_task(sre_seq, agent_type="sre", status="completed",
                                 result_text=json.dumps({"conclusion": "SRE ok"}, ensure_ascii=False))
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT id FROM issue WHERE repo_key=? AND issue_number=?", (REPO_KEY, 103))
                issue_id = c.fetchone()[0]
            r = client.get(f"/api/issues/{issue_id}/pipeline", headers=_headers)
            assert r.status_code == 200
            d = r.json()
            assert d["review_status"] == "pending_review"
            assert d["issue_stage"] == "pending_review"
            assert d["issue_task_status"] == "pending_review"
            assert d["sre_result"]["task_ref"]["task_id"] == task_id
            assert d["sre_result"]["result_ref"]["status"] == "available"
            assert "SRE ok" in d["sre_result"]["result_ref"]["summary"]
        finally:
            _clean()


# ===================== 场景 2：验收失败 =====================

class TestAcceptanceFailedScenario:
    def test_sre_task_failed_no_result(self):
        _seed_issue(111, stage="sre", status="processing")
        sre_seq = _mk_event(111, "sre", status="failed")
        _mk_agent_task(sre_seq, agent_type="sre", status="failed", result_text="")
        try:
            r = _get_pipeline(111)
            d = r.json()
            # 需求仍在流水线（验收失败 → 打回/待处理），不是 completed
            assert d["review_status"] == "in_progress"
            sre_seg = [s for s in d["segments"] if s["agent"] == "sre"][-1]
            # 任务状态显式标注 failed；无结果 → unavailable（不泄漏 errors/stderr）
            assert sre_seg["output"]["task_ref"]["task_status"] == "failed"
            assert sre_seg["output"]["result_ref"]["status"] == "unavailable"
            assert "summary" not in sre_seg["output"]["result_ref"]
        finally:
            _clean()

    def test_sre_task_failed_with_result(self):
        _seed_issue(112, stage="sre", status="processing")
        sre_seq = _mk_event(112, "sre", status="completed")
        _mk_agent_task(sre_seq, agent_type="sre", status="failed",
                       result_text="验收失败：部署健康检查未通过")
        try:
            d = _get_pipeline(112).json()
            sre_seg = [s for s in d["segments"] if s["agent"] == "sre"][-1]
            assert sre_seg["output"]["task_ref"]["task_status"] == "failed"
            assert sre_seg["output"]["result_ref"]["status"] == "available"
            assert "验收失败" in sre_seg["output"]["result_ref"]["summary"]
        finally:
            _clean()


# ===================== 场景 3：取消 =====================

class TestCancelledScenario:
    def test_cancelled(self):
        _seed_issue(121, stage="coding", status="cancelled")
        _mk_event(121, "coding", status="cancelled")
        try:
            d = _get_pipeline(121).json()
            # 旧字段：最后事件执行状态
            assert d["status"] == "cancelled"
            assert d["review_status"] == "cancelled"
        finally:
            _clean()


# ===================== 场景 4：生产已完成 =====================

class TestCompletedScenario:
    def test_production_completed(self):
        _seed_issue(131, stage="done", status="completed")
        for agent in ("vod", "issue", "coding", "qa", "sre"):
            _mk_event(131, agent, status="completed")
        try:
            d = _get_pipeline(131).json()
            assert d["status"] == "completed"
            assert d["review_status"] == "completed"
            assert d["issue_stage"] == "done"
        finally:
            _clean()


# ===================== 场景 5：生产尚未开始 =====================

class TestNotStartedScenario:
    def test_in_progress_no_sre(self):
        _seed_issue(141, stage="coding", status="processing")
        _mk_event(141, "coding", status="running")
        try:
            d = _get_pipeline(141).json()
            assert d["review_status"] == "in_progress"
            segs = [s for s in d["segments"] if s["agent"] == "sre"]
            assert segs == []  # SRE 尚未开始
        finally:
            _clean()

    def test_running_task_pending_result(self):
        _seed_issue(142, stage="sre", status="processing")
        sre_seq = _mk_event(142, "sre", status="running")
        _mk_agent_task(sre_seq, agent_type="sre", status="running", result_text="")
        try:
            d = _get_pipeline(142).json()
            sre_seg = [s for s in d["segments"] if s["agent"] == "sre"][-1]
            # 在途任务 → pending 显式枚举
            assert sre_seg["output"]["result_ref"]["status"] == "pending"
            assert sre_seg["output"]["task_ref"]["task_status"] == "running"
        finally:
            _clean()


# ===================== 场景 6：旧字段兼容性 =====================

class TestBackwardCompatibility:
    def test_old_fields_unchanged_and_new_fields_optional(self):
        _seed_issue(151, stage="qa", status="processing")
        seq = _mk_event(151, "qa", status="completed")
        try:
            d = _get_pipeline(151).json()
            # 旧顶层字段语义与结构不变
            for k in ("issue", "summary", "status", "segments"):
                assert k in d
            # 旧 output 字段不变
            out = d["segments"][-1]["output"]
            for k in ("conclusion", "next_step", "actions", "artifacts", "issue_summary"):
                assert k in out
            # 新增字段可选存在，无任务 → 显式 unavailable 枚举（非空值/缺省）
            assert d["review_status"] == "in_progress"
            assert out["task_ref"] == {"task_id": None, "task_status": "unavailable", "query_path": ""}
            assert out["result_ref"] == {"status": "unavailable"}
            # 事件 seq 与任务 event_seq 不匹配 → 仍 unavailable
            assert seq > 0
        finally:
            _clean()

    def test_issue_without_issue_row_falls_back(self):
        # 无 issue 卡片行、仅有事件：review_status 显式 unknown，不报错
        _mk_event(152, "coding", status="completed")
        try:
            r = _get_pipeline(152)
            assert r.status_code == 200
            d = r.json()
            assert d["review_status"] == "unknown"
        finally:
            _clean()


# ===================== 场景 7：脱敏 =====================

class TestNoSensitiveLeak:
    def test_result_summary_redacted_in_pipeline(self):
        _seed_issue(161, stage="pending_review", status="pending_review")
        sre_seq = _mk_event(161, "sre", status="completed")
        _mk_agent_task(
            sre_seq, agent_type="sre", status="completed",
            result_text=json.dumps({
                "conclusion": "验收通过；内部凭据 tk_abcdef1234567890 已轮换",
            }, ensure_ascii=False))
        try:
            d = _get_pipeline(161).json()
            sre_seg = [s for s in d["segments"] if s["agent"] == "sre"][-1]
            rr = sre_seg["output"]["result_ref"]
            assert rr["status"] == "available"
            assert "tk_abcdef1234567890" not in json.dumps(rr, ensure_ascii=False)
            assert "[REDACTED]" in rr["summary"]
            # result_ref 仅引用+摘要，不内嵌全量 result_text
            assert "result_text" not in rr
        finally:
            _clean()
