"""Issue #1739：流水线 QA 失败时 Issue 不得因 PR 提交/合入被自动关闭。

覆盖三类场景 + 关闭路径：
- qa fail（status=failed）→ 自动结单不关闭、Issue 保持可处理；
- qa fail（status=completed + “不通过”文本）→ 门禁识别并阻断；
- qa pass → 正常自动结单并关闭 VCS；
- 流水线无 qa 节点 → 关闭行为不变；
- review-pass(done) 在 qa fail 时 noop:qa_failed；
- 建 PR 在 qa fail 时强制 auto_close_issues=false；
- 合入 PR 在 qa fail 时 409 阻断。
"""
import os
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

_REPO = "gitcode:org/issue1739"
_NUM = 1739
_ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                  "qa": "completed", "sre": "completed"}


def setup_module():
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
            "VALUES (?,?,?,?,?,?)",
            (_REPO, "org", "issue1739", "org/issue1739", 1, "bot_issue1739_token"))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO,))
        db.commit()


def _seed(status="processing", stage="sre", scene_id=""):
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute(
            "INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, status, "
            "completed_at, completed_reason, scene_id) VALUES (0, '', ?, ?, ?, NULL, NULL, ?)",
            (_REPO, _NUM, status, scene_id))
        db.commit()
    set_issue_stage(_REPO, f"#{_NUM}", stage, stage)


def _seed_events(statuses: dict, qa_result="", scene_id=""):
    with db_lock:
        c = db.cursor()
        for _ag, _st in statuses.items():
            _res = qa_result if _ag == "qa" else ""
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, result, scene_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (_REPO, f"#{_NUM}", "a2a_event", _ag, _st, _res, scene_id))
        db.commit()


def _add_event(agent_type: str, status: str, result: str = "", scene_id: str = ""):
    """按调用顺序追加事件（seq 递增），用于构造跨流水线时序。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, result, scene_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (_REPO, f"#{_NUM}", "a2a_event", agent_type, status, result, scene_id))
        db.commit()


def _issue_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_at, completed_reason, stage FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        return c.fetchone()


def _clear_annotation_cache():
    from src.api import qa_gate
    qa_gate._annotated.clear()


# ===================== 统一判定 =====================

def test_classify_qa_conclusion_text_forms():
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("completed", "审查完成：不通过（2 项）") == "fail"
    assert classify_qa_conclusion("completed", "Spec: 1 阻塞 + 2 次要") == "fail"
    assert classify_qa_conclusion("completed", "通过（2 条非阻塞建议）") == "pass"
    assert classify_qa_conclusion("completed", "pass，无 Critical/Required") == "pass"
    assert classify_qa_conclusion("failed", "[skill_mismatch]") == "fail"
    assert classify_qa_conclusion("running", "") == "unknown"


def test_classify_qa_conclusion_cancelled_is_fail():
    """Spec：status=cancelled 视为未通过，不得放行自动关闭。"""
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("cancelled", "") == "fail"
    assert classify_qa_conclusion("canceled", "") == "fail"


def test_classify_qa_conclusion_negation_forms():
    """否定语境不得误判 fail，“无法通过”不得误判 pass。"""
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("completed", "无失败，0 failure，无阻塞") != "fail"
    assert classify_qa_conclusion("completed", "no fixes required") != "fail"
    assert classify_qa_conclusion("completed", "no failures") != "fail"
    assert classify_qa_conclusion("completed", "无法通过验收") == "fail"
    assert classify_qa_conclusion("completed", "not passed") == "fail"
    assert classify_qa_conclusion("completed", "no blocking issues, pass") == "pass"


def test_qa_gate_blocks_completed_but_failed_text():
    _clear_annotation_cache()
    from src.api.qa_gate import qa_gate_check
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="PR #9 审查完成：不通过（1 项阻塞）")
    info = qa_gate_check(_REPO, _NUM)
    assert info["blocked"] is True
    assert info["conclusion"] == "fail"
    assert info["event_seq"] > 0


def test_qa_gate_blocks_cancelled_event():
    from src.api.qa_gate import qa_gate_check
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="")
    with db_lock:
        db.cursor().execute(
            "UPDATE events SET status='cancelled' WHERE repo_key=? AND agent_type='qa'",
            (_REPO,))
        db.commit()
    info = qa_gate_check(_REPO, _NUM)
    assert info["blocked"] is True
    assert info["conclusion"] == "fail"


def test_qa_gate_ignores_other_pipeline_events():
    """跨流水线（不同 scene_id）的 qa 结论不得误拦当前流水线。"""
    from src.api.qa_gate import qa_gate_check
    _clear_annotation_cache()
    _seed(scene_id="scene-A")
    _add_event("qa", "completed", "pass，无阻塞", scene_id="scene-A")
    _add_event("qa", "failed", "不通过", scene_id="scene-B")
    _cfg = {"nodes": [{"id": "qa", "next": "done"}]}
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_cfg):
        info = qa_gate_check(_REPO, _NUM)
    assert info["qa_present"] is True
    assert info["blocked"] is False, info


# ===================== 自动结单 =====================

def test_auto_finalize_blocked_when_qa_failed_status():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _clear_annotation_cache()
    _seed()
    _seed_events({**_ALL_COMPLETED, "qa": "failed"}, qa_result="[skill_mismatch] qa 未执行")
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res in ("pending_review_fallback", "qa_failed_blocked"), _res
        _cli.return_value.close_issue.assert_not_called()
    _st, _at, _reason, _stage = _issue_row()
    assert _st != "completed"
    assert not _at


def test_auto_finalize_blocked_when_qa_completed_but_failed_text():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="PR #9 审查完成：不通过（1 项阻塞）")
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "qa_failed_blocked", _res
        _cli.return_value.close_issue.assert_not_called()
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "processing"
    assert _stage == "sre"
    assert not _at


def test_auto_finalize_closes_when_qa_passed():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="pass，无 Critical/Required")
    with patch("src.vcs.api_client.VcsApiClient") as _cli:
        _inst = _cli.return_value
        _inst.get_issue_state.return_value = "open"
        _inst.close_issue.return_value = {"ok": True}
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "auto_done", _res
        _inst.close_issue.assert_called_once_with(_NUM)
    _st, _at, _reason, _stage = _issue_row()
    assert _st == "completed"
    assert _stage == "done"
    assert _reason == "auto_finalized"


def test_auto_finalize_closes_when_pipeline_has_no_qa_node():
    from src.api.issues_center import auto_finalize_issue_on_last_node
    _clear_annotation_cache()
    _seed()
    _seed_events({k: v for k, v in _ALL_COMPLETED.items() if k != "qa"})
    _no_qa_cfg = {"nodes": [
        {"id": "vod", "next": "issue"}, {"id": "issue", "next": "coding"},
        {"id": "coding", "next": "sre"}, {"id": "sre", "next": "done"},
    ]}
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=_no_qa_cfg), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _inst = _cli.return_value
        _inst.get_issue_state.return_value = "open"
        _inst.close_issue.return_value = {"ok": True}
        _res = auto_finalize_issue_on_last_node(_REPO, f"#{_NUM}", "sre", source="test")
        assert _res == "auto_done", _res
        _inst.close_issue.assert_called_once_with(_NUM)
    assert _issue_row()[0] == "completed"


# ===================== 审核收尾 =====================

def test_review_pass_done_blocked_when_qa_failed():
    from src.api.issues_center import _review_pass
    _clear_annotation_cache()
    _seed(status="processing", stage="sre")
    with db_lock:
        db.cursor().execute("UPDATE issue SET status='pending_review' "
                            "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        db.commit()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    with patch("src.vcs.api_client.VcsApiClient"):
        _res = _review_pass(_REPO, _NUM, target="done", evidence="test")
    assert _res == "noop:qa_failed", _res
    assert _issue_row()[0] == "pending_review"


def test_review_pass_processing_not_blocked_when_qa_failed():
    """qa 失败后继续处理（resume）不应被门禁阻断。"""
    from src.api.issues_center import _review_pass
    _clear_annotation_cache()
    _seed(status="processing", stage="coding")
    with db_lock:
        db.cursor().execute("UPDATE issue SET status='pending_review' "
                            "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        db.commit()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    with patch("src.vcs.api_client.VcsApiClient"):
        _res = _review_pass(_REPO, _NUM, target="processing", evidence="resume")
    assert _res == "passed_processing", _res
    assert _issue_row()[0] == "processing"


# ===================== PR 创建 / 合入 =====================

def test_create_pull_forces_auto_close_false_when_qa_failed():
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull
    captured = {}

    def _fake_vcs_req(provider, method, url, token, body=None):
        captured["payload"] = body
        return {"number": 9, "html_url": "https://example/9"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch.object(git_repo_api, "_provider", "gitcode"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="close #1739", related_issue_nums=[_NUM], auto_close_issues=True),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="gitcode")
    assert captured["payload"]["close_related_issue"] is False
    _comment = _cli.return_value.post_issue_comment.call_args[0][1]
    assert "QA 未通过" in _comment
    assert "不通过" in _comment
    assert "#9" in _comment


def test_annotation_dedupes_by_existing_remote_marker():
    """跨进程/重启：Issue 已有相同幂等标记的评论时不再重复标注。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from src.api import git_repo_api, qa_gate
    from src.api.git_repo_api import PRCreateBody, create_pull

    _info = qa_gate.qa_gate_check(_REPO, _NUM)
    _token = qa_gate._annotation_token(_info["event_seq"], _info["status"], _info["reason"])

    def _fake_vcs_req(provider, method, url, token, body=None):
        return {"number": 9}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = [
            {"body": f"之前的门禁评论\n{_token}"}]
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="close #1739", related_issue_nums=[_NUM], auto_close_issues=True),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="gitcode")
    _cli.return_value.post_issue_comment.assert_not_called()


def test_create_pull_keeps_auto_close_true_when_qa_passed():
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="pass")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull
    captured = {}

    def _fake_vcs_req(provider, method, url, token, body=None):
        captured["payload"] = body
        return {"number": 9}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.vcs.api_client.VcsApiClient"):
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="close #1739", related_issue_nums=[_NUM], auto_close_issues=True),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="gitcode")
    assert captured["payload"]["close_related_issue"] is True


def test_merge_blocked_when_qa_failed():
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from fastapi import HTTPException

    from src.api import git_repo_api
    from src.api.git_repo_api import PRMergeBody, merge_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "GET" and "/pulls/9" in url:
            return {"number": 9, "body": "关闭 Issue #1739", "state": "open"}
        return {"merged": True, "sha": "abc"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.get_pull_base", return_value={"ref": "weekly-mys", "sha": "b"}), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        with pytest.raises(HTTPException) as _exc:
            merge_pull("org", "issue1739", 9, PRMergeBody(), user={
                "id": 1, "username": "tester", "auth_type": "session_token"}, p="gitcode")
    assert _exc.value.status_code == 409
    assert _exc.value.detail["gate"]["phase"] == "pre_merge_qa"
    _comment = _cli.return_value.post_issue_comment.call_args[0][1]
    assert "不通过" in _comment
    assert "#9" in _comment


def test_merge_blocked_by_pr_issue_association_without_body_ref():
    """PR body 不含 #N，但 PR 详情 issues 关联 Issue 时仍须门禁（inner_issue_nums 旁路）。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from fastapi import HTTPException

    from src.api import git_repo_api
    from src.api.git_repo_api import PRMergeBody, merge_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "GET" and "/pulls/9" in url:
            return {"number": 9, "body": "仅描述改动，无 Issue 引用",
                    "issues": [{"number": _NUM}], "state": "open"}
        return {"merged": True, "sha": "abc"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.get_pull_base", return_value={"ref": "weekly-mys", "sha": "b"}), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        with pytest.raises(HTTPException) as _exc:
            merge_pull("org", "issue1739", 9, PRMergeBody(), user={
                "id": 1, "username": "tester", "auth_type": "session_token"}, p="gitcode")
    assert _exc.value.status_code == 409
    assert _exc.value.detail["gate"]["issue"] == _NUM


def test_merge_fail_open_on_pr_detail_upstream_error():
    """PR 详情 404/502 属上游临时错误，门禁不得中断合入（fail-open）。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from fastapi import HTTPException

    from src.api import git_repo_api
    from src.api.git_repo_api import PRMergeBody, merge_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "GET" and "/pulls/9" in url:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "upstream"})
        return {"merged": True, "sha": "abc"}

    captured = {}

    def _record_gate_log(*a, **k):
        captured["log"] = a

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.get_pull_base", return_value={"ref": "weekly-mys", "sha": "b"}), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.run_post_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log", side_effect=_record_gate_log), \
            patch("src.vcs.api_client.VcsApiClient"):
        _res = merge_pull("org", "issue1739", 9, PRMergeBody(), user={
            "id": 1, "username": "tester", "auth_type": "session_token"}, p="gitcode")
    assert _res.get("merged") is True

# ===================== 复审补充（PR #1094 round 3） =====================

def test_classify_qa_conclusion_trailing_negation_forms():
    """英文“critical: none / not required / required: none”不得误判 fail。"""
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("completed", "critical: none") != "fail"
    assert classify_qa_conclusion("completed", "not required") != "fail"
    assert classify_qa_conclusion("completed", "required: none") != "fail"
    assert classify_qa_conclusion("completed", "critical issue found") == "fail"


def test_qa_gate_fail_open_on_scene_resolve_error():
    """场景解析异常必须放行，不得退化为 default 流水线判定。"""
    from src.api.qa_gate import qa_gate_check
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    with patch("src.api.session_store.get_issue_scene",
               side_effect=RuntimeError("scene store unavailable")):
        info = qa_gate_check(_REPO, _NUM)
    assert info["blocked"] is False, info
    assert info["qa_present"] is False


def test_github_create_pull_uses_related_when_auto_close_off():
    """qa fail 时 GitHub 关联降级为 Related，不注入 Closes（避免 UI 合入自动关闭）。"""
    from src.vcs.providers import translate_inbound
    payload = translate_inbound("github", "pulls", "create", {
        "title": "t", "head": "h", "base": "b", "body": "body",
        "related_issue_nums": [1739], "auto_close_issues": False,
    }, {"owner_repo": "org/repo"})
    assert "Closes #1739" not in payload["body"]
    assert "Related to #1739" in payload["body"]
    payload2 = translate_inbound("github", "pulls", "create", {
        "title": "t", "head": "h", "base": "b", "body": "body",
        "related_issue_nums": [1739], "auto_close_issues": True,
    }, {"owner_repo": "org/repo"})
    assert "Closes #1739" in payload2["body"]


def _seed_failed_issue(issue_number: int):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, status, result, scene_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (_REPO, f"#{issue_number}", "a2a_event", "qa", "completed",
             "审查完成：不通过", ""))
        db.commit()


def test_create_pull_annotates_all_failed_issues():
    """多 Issue PR：所有 qa 失败的关联 Issue 都要标注，而非只标首个。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    _seed_failed_issue(1740)
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        return {"number": 9}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="multi", related_issue_nums=[_NUM, 1740], auto_close_issues=True),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="gitcode")
    _commented = [c[0][0] for c in _cli.return_value.post_issue_comment.call_args_list]
    assert _NUM in _commented and 1740 in _commented, _commented


def test_merge_annotates_all_failed_issues():
    """多 Issue PR 合入阻断：所有 qa 失败 Issue 都标注，409 列出全部。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    _seed_failed_issue(1740)
    from fastapi import HTTPException

    from src.api import git_repo_api
    from src.api.git_repo_api import PRMergeBody, merge_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "GET" and "/pulls/9" in url:
            return {"number": 9, "body": "close #1739 #1740", "state": "open"}
        return {"merged": True, "sha": "abc"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.get_pull_base", return_value={"ref": "weekly-mys", "sha": "b"}), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        with pytest.raises(HTTPException) as _exc:
            merge_pull("org", "issue1739", 9, PRMergeBody(), user={
                "id": 1, "username": "tester", "auth_type": "session_token"}, p="gitcode")
    assert _exc.value.status_code == 409
    assert sorted(_exc.value.detail["gate"]["issues"]) == [_NUM, 1740]
    _commented = [c[0][0] for c in _cli.return_value.post_issue_comment.call_args_list]
    assert _NUM in _commented and 1740 in _commented, _commented


# ===================== 复审补充（PR #1094 round 4） =====================

def test_classify_qa_conclusion_chinese_negated_pass_is_fail():
    """中文否定式通过（没有/没/未/不）不得被“通过”吞成 pass。"""
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("completed", "没有通过测试") == "fail"
    assert classify_qa_conclusion("completed", "没通过") == "fail"
    assert classify_qa_conclusion("completed", "没有通过") == "fail"
    assert classify_qa_conclusion("completed", "未通过") == "fail"
    assert classify_qa_conclusion("completed", "不通过") == "fail"
    assert classify_qa_conclusion("completed", "无法通过") == "fail"


def test_classify_qa_conclusion_chinese_trailing_negation():
    """中文后置否定（阻塞为零 / 阻塞 0 / 无不通过项）不得误判 fail。"""
    from src.api.qa_gate import classify_qa_conclusion
    assert classify_qa_conclusion("completed", "阻塞为零") != "fail"
    assert classify_qa_conclusion("completed", "阻塞 0") != "fail"
    assert classify_qa_conclusion("completed", "无不通过项") != "fail"
    assert classify_qa_conclusion("completed", "阻塞 1 项") == "fail"
    assert classify_qa_conclusion("completed", "阻断: 0 项") != "fail"


def test_github_create_pull_strips_caller_closes_when_auto_close_off():
    """调用方 body 自带 Closes #N 时，auto_close=false 必须清除并降级为 Related。"""
    from src.vcs.providers import translate_inbound
    payload = translate_inbound("github", "pulls", "create", {
        "title": "t", "head": "h", "base": "b", "body": "Closes #12",
        "related_issue_nums": [12], "auto_close_issues": False,
    }, {"owner_repo": "org/repo"})
    assert "Closes #12" not in payload["body"]
    assert "Related to #12" in payload["body"]
    payload2 = translate_inbound("github", "pulls", "create", {
        "title": "t", "head": "h", "base": "b", "body": "Closes #12",
        "related_issue_nums": [12], "auto_close_issues": True,
    }, {"owner_repo": "org/repo"})
    assert payload2["body"] == "Closes #12"


def test_create_pull_gates_and_annotates_when_auto_close_default_false():
    """默认 auto_close_issues=false 时仍须校验并标注 QA 失败（AC1/AC2）。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull

    def _fake_vcs_req(provider, method, url, token, body=None):
        return {"number": 9}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.vcs.api_client.VcsApiClient") as _cli:
        _cli.return_value.list_issue_comments.return_value = []
        _cli.return_value.post_issue_comment.return_value = {"id": 1}
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="multi", related_issue_nums=[_NUM]),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="gitcode")
    _cli.return_value.post_issue_comment.assert_called_once()
    _comment = _cli.return_value.post_issue_comment.call_args[0][1]
    assert "QA 未通过" in _comment


# ===================== 复审补充（PR #1094 round 5） =====================

def test_github_create_pull_keeps_closes_when_qa_passed():
    """AC3：qa 通过时 GitHub 沿用原语义，追加 Closes 而非 Related。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="pass，无 Critical/Required")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull
    captured = {}

    def _fake_vcs_req(provider, method, url, token, body=None):
        captured["payload"] = body
        return {"number": 1094}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.qa_gate.collect_qa_failures", return_value=[]), \
            patch("src.vcs.api_client.VcsApiClient"):
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="feat: #1739 test", related_issue_nums=[_NUM]),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="github")
    _body = captured["payload"]["body"]
    assert "Closes #1739" in _body
    assert "Related to" not in _body


def test_github_create_pull_downgrades_to_related_when_qa_failed():
    """qa 失败时 GitHub 仅 Related 关联，且清除调用方 body 自带的 Closes。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="审查完成：不通过")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRCreateBody, create_pull
    captured = {}

    def _fake_vcs_req(provider, method, url, token, body=None):
        captured["payload"] = body
        return {"number": 1094}

    _info = {"blocked": True, "qa_present": True, "event_seq": 1,
             "status": "completed", "conclusion": "fail",
             "reason": "审查完成：不通过"}
    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.qa_gate.collect_qa_failures",
                  return_value=[{"issue": _NUM, "info": _info}]), \
            patch("src.api.qa_gate.annotate_qa_failures"), \
            patch("src.vcs.api_client.VcsApiClient"):
        create_pull("org", "issue1739", PRCreateBody(
            title="feat: #1739 test", head="feat/issue-1739", base="weekly-mys",
            body="Closes #1739", related_issue_nums=[_NUM]),
            user={"id": 1, "username": "tester", "auth_type": "session_token"},
            p="github")
    _body = captured["payload"]["body"]
    assert "Closes #1739" not in _body
    assert "Related to #1739" in _body


def test_pr_link_uses_provider_specific_path():
    """GitCode 关联 PR 链接必须用 /merge_requests/{n}（/pull 会 404）。"""
    from src.api.qa_gate import _pr_link
    assert _pr_link(_REPO, 1094).endswith("/merge_requests/1094")
    assert _pr_link("github:org/issue1739", 1094).endswith("/pull/1094")


def test_review_pass_done_runs_qa_gate_once():
    """review-pass(done) 收尾只查询/标注一次 QA 门禁。"""
    from src.api import qa_gate
    from src.api.issues_center import _review_pass
    _clear_annotation_cache()
    _seed(status="processing", stage="sre")
    with db_lock:
        db.cursor().execute("UPDATE issue SET status='pending_review' "
                            "WHERE repo_key=? AND issue_number=?", (_REPO, _NUM))
        db.commit()
    _seed_events(_ALL_COMPLETED, qa_result="pass")
    with patch.object(qa_gate, "gate_or_annotate",
                      wraps=qa_gate.gate_or_annotate) as _g, \
            patch("src.vcs.api_client.VcsApiClient"):
        _res = _review_pass(_REPO, _NUM, target="done", evidence="test")
    assert _res == "passed_done", _res
    assert _g.call_count == 1, _g.call_count


def test_merge_reuses_single_pull_detail_request():
    """合入路径 base 快照与 QA 门禁复用同一次 PR 详情请求。"""
    _clear_annotation_cache()
    _seed()
    _seed_events(_ALL_COMPLETED, qa_result="pass")
    from src.api import git_repo_api
    from src.api.git_repo_api import PRMergeBody, merge_pull

    _get_calls = []

    def _fake_vcs_req(provider, method, url, token, body=None):
        if method == "GET" and "/pulls/9" in url:
            _get_calls.append(url)
            return {"number": 9, "body": "close #1739", "state": "open",
                    "base": {"ref": "weekly-mys", "sha": "b"}}
        return {"merged": True, "sha": "abc"}

    with patch.object(git_repo_api, "_vcs_req", _fake_vcs_req), \
            patch.object(git_repo_api, "_repo_token", lambda *a, **k: "tok"), \
            patch("src.api.merge_gate.run_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.run_post_merge_gate", return_value={"decision": "passed"}), \
            patch("src.api.merge_gate.record_gate_log"), \
            patch("src.vcs.api_client.VcsApiClient"):
        _res = merge_pull("org", "issue1739", 9, PRMergeBody(), user={
            "id": 1, "username": "tester", "auth_type": "session_token"}, p="gitcode")
    assert _res.get("merged") is True
    assert len(_get_calls) == 1, _get_calls
