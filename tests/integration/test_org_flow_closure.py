"""Issue #2251: 组织下派完成判定与组织流水线闭环集成测试。

覆盖：completed / pending_review→needs_review / awaiting_input / failed-timeout /
done_when=pr_merged / done_when=task_terminal / 未终态不提前 pending_review /
重复 sweep 幂等。

约束：复用 ``tests/integration/conftest.py::org_e2e``（真实 DB + 真实
``org_dispatch`` 状态机，仅 VCS adapter mock）；子任务状态经真实状态迁移推进，
``done_when`` 作为 target 配置列直接落库；不直改 dispatch status、不直调聚合。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

TARGET_A_KEY = "gitcode:issue2251/svc-a"
TARGET_B_KEY = "gitcode:issue2251/svc-b"
SCENE_TARGET_A = "scene-2251-svc-a"
SCENE_TARGET_B = "scene-2251-svc-b"


@pytest.fixture
def posted_comments(monkeypatch):
    """捕获组织 Issue 聚合评论（VCS adapter seam，不触网）。"""
    posted: list[tuple[int, str]] = []
    from src.domains.runtime.vcs.api_client import VcsApiClient

    def _fake_post(self, issue_number, body):
        posted.append((int(issue_number), body or ""))
        return {"ok": True}

    monkeypatch.setattr(VcsApiClient, "post_issue_comment", _fake_post)
    return posted


def _bootstrap(e2e, slug: str, issue_number: int) -> dict:
    e2e.create_org(slug)
    e2e.link_target_repo(TARGET_A_KEY, SCENE_TARGET_A)
    e2e.link_target_repo(TARGET_B_KEY, SCENE_TARGET_B)
    e2e.create_org_issue(number=issue_number, title="闭环组织需求")
    resp = e2e.dispatch([TARGET_A_KEY, TARGET_B_KEY], org_issue_number=issue_number)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _targets_by_repo(detail: dict) -> dict:
    return {t["repo_key"]: t for t in detail["targets"]}


def _set_done_when(dispatch_id: int, repo_key: str, done_when: str) -> None:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET done_when=? WHERE dispatch_id=? AND repo_key=?",
            (done_when, dispatch_id, repo_key),
        )
        db.commit()


def _set_issue_status(repo_key: str, issue_number: int, status: str) -> None:
    """真实运行态写入口的等价 SQL（awaiting_input 只在 runtime 路由内联写入）。"""
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status=? WHERE repo_key=? AND issue_number=?",
            (status, repo_key, issue_number),
        )
        db.commit()


def _set_pending_review(repo_key: str, issue_number: int) -> None:
    from src.api.issues_center import _set_issue_pending_review

    assert _set_issue_pending_review(repo_key, issue_number), (
        f"child {repo_key}#{issue_number} not moved to pending_review")


def _seed_task(repo_key: str, issue_number: int, status: str) -> None:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, "
            "issue_id, status) VALUES (?,?,?,?,?,?,?)",
            (f"t-2251-{repo_key}-{issue_number}-{status}", "sess-2251", 1, "p",
             repo_key, f"#{issue_number}", status),
        )
        db.commit()


def _append_merge_event(repo_key: str, issue_number: int, pr_number: int = 77) -> None:
    from src.api.event_store import append_event

    append_event(
        repo_key=repo_key, event_type="pull_request_merged", issue_id=f"#{issue_number}",
        title=f"PR {pr_number} 已合并", payload={"pull_request": {"number": pr_number}},
        status="completed",
    )


def _read_org_issue_status(e2e, issue_number: int) -> str:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        row = db.cursor().execute(
            "SELECT status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (e2e.org_repo_key, issue_number),
        ).fetchone()
    return (row[0] if row else "") or ""


def _aggregate_events(dispatch_id: str) -> list[tuple]:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        return db.cursor().execute(
            "SELECT seq, status, idempotency_key, payload FROM events "
            "WHERE event_type='a2a_event' AND idempotency_key LIKE ? ORDER BY seq",
            (f"org_agg:{dispatch_id}:%",),
        ).fetchall()


def _assert_aggregate_comment(posted_comments, org_issue_number: int, *needles: str) -> str:
    matched = [b for n, b in posted_comments if int(n) == int(org_issue_number)]
    assert matched, f"未写入组织 Issue #{org_issue_number} 聚合评论: {posted_comments}"
    body = matched[-1]
    for needle in needles:
        assert needle in body, f"聚合评论缺少 {needle!r}: {body}"
    return body


def test_all_completed_closes_dispatch_and_org_issue(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-done", 22510)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    for key in (TARGET_A_KEY, TARGET_B_KEY):
        e2e.complete_child_issue(key, by_repo[key]["child_issue_number"])
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "completed", detail
    assert all(t["status"] == "completed" for t in detail["targets"])

    assert _read_org_issue_status(e2e, 22510) == "pending_review"
    _assert_aggregate_comment(
        posted_comments, 22510, "completed", TARGET_A_KEY, TARGET_B_KEY)

    events = _aggregate_events(dispatch_id)
    assert len(events) == 1, events
    assert events[0][1] == "queued", events

    # 重复 sweep：聚合触发/评论保持 1 次
    e2e.sweep()
    e2e.sweep()
    assert len(_aggregate_events(dispatch_id)) == 1
    assert len([1 for n, _ in posted_comments if int(n) == 22510]) == 1


def test_pending_review_becomes_needs_review_not_infinite_running(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-review", 22511)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    _set_pending_review(TARGET_B_KEY, by_repo[TARGET_B_KEY]["child_issue_number"])
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    targets = _targets_by_repo(detail)
    assert targets[TARGET_A_KEY]["status"] == "completed"
    assert targets[TARGET_B_KEY]["status"] == "needs_review", targets[TARGET_B_KEY]
    assert detail["status"] == "needs_review", detail
    assert _read_org_issue_status(e2e, 22511) == "pending_review"
    _assert_aggregate_comment(posted_comments, 22511, "needs_review", TARGET_B_KEY)


def test_awaiting_input_also_terminal_as_needs_review(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-input", 22512)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    _set_issue_status(TARGET_B_KEY, by_repo[TARGET_B_KEY]["child_issue_number"], "awaiting_input")
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    assert _targets_by_repo(detail)[TARGET_B_KEY]["status"] == "needs_review"
    assert detail["status"] == "needs_review"


def test_running_target_does_not_preclose_org_issue(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-running", 22513)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "running", detail
    assert _read_org_issue_status(e2e, 22513) == "active"
    assert not [b for n, b in posted_comments if int(n) == 22513]
    assert not _aggregate_events(dispatch_id)


def test_timeout_failure_aggregates_with_failure_list(org_e2e, posted_comments, monkeypatch):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-failed", 22514)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    from src.domains.mgmt import org_dispatch

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    monkeypatch.setattr(org_dispatch, "TARGET_TIMEOUT_SECONDS", 0)
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    targets = _targets_by_repo(detail)
    assert detail["status"] == "partial_failed", detail
    assert targets[TARGET_B_KEY]["status"] == "failed"
    assert "timeout" in targets[TARGET_B_KEY]["error"]
    assert _read_org_issue_status(e2e, 22514) == "pending_review"
    _assert_aggregate_comment(posted_comments, 22514, "failed", TARGET_B_KEY)


def test_done_when_pr_merged_waits_for_merge_event(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-pr", 22515)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    _set_done_when(dispatch_id, TARGET_A_KEY, "pr_merged")
    _set_done_when(dispatch_id, TARGET_B_KEY, "pr_merged")
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        _set_pending_review(key, by_repo[key]["child_issue_number"])
    e2e.sweep()

    # 只有 pending_review、无合入事件 → 不算完成，dispatch 仍在途
    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "running", detail
    assert _read_org_issue_status(e2e, 22515) == "active"

    # 两仓库 PR 合入 → 终态 completed
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        _append_merge_event(key, by_repo[key]["child_issue_number"])
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "completed", detail
    targets = _targets_by_repo(detail)
    assert all(t["status"] == "completed" for t in targets.values())
    assert all(t["pr_number"] == 77 for t in targets.values()), targets
    _assert_aggregate_comment(posted_comments, 22515, "completed", "77")


def test_done_when_task_terminal(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-task", 22516)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    _set_done_when(dispatch_id, TARGET_A_KEY, "task_terminal")
    _set_done_when(dispatch_id, TARGET_B_KEY, "task_terminal")
    _seed_task(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"], "completed")
    _seed_task(TARGET_B_KEY, by_repo[TARGET_B_KEY]["child_issue_number"], "failed")
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    targets = _targets_by_repo(detail)
    assert targets[TARGET_A_KEY]["status"] == "completed", targets[TARGET_A_KEY]
    assert targets[TARGET_B_KEY]["status"] == "failed", targets[TARGET_B_KEY]
    assert detail["status"] == "partial_failed", detail
    _assert_aggregate_comment(posted_comments, 22516, "failed", TARGET_B_KEY)


def test_done_when_pr_merged_timeout_becomes_needs_review_not_failed(
        org_e2e, posted_comments, monkeypatch):
    """Issue #2251 复审：pr_merged 等人工合入超时不得判 failed。"""
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-pr-timeout", 22518)
    dispatch_id = body["dispatch_id"]

    from src.domains.mgmt import org_dispatch

    for key in (TARGET_A_KEY, TARGET_B_KEY):
        _set_done_when(dispatch_id, key, "pr_merged")
    monkeypatch.setattr(org_dispatch, "TARGET_TIMEOUT_SECONDS", 0)
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    targets = _targets_by_repo(detail)
    assert detail["status"] == "needs_review", detail
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        assert targets[key]["status"] == "needs_review", targets[key]
        assert "等待合入超时" in targets[key]["error"], targets[key]
    assert _read_org_issue_status(e2e, 22518) == "pending_review"
    body_text = _assert_aggregate_comment(posted_comments, 22518, "needs_review")
    assert "failed" not in body_text, body_text


def test_unknown_target_status_does_not_silently_complete(org_e2e):
    """未知 target 状态不得静默判 completed（复审 Optional）。"""
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-unknown", 22519)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    from src.kernel.session_manager import db, db_lock

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='aggregating' "
            "WHERE dispatch_id=? AND repo_key=?",
            (dispatch_id, TARGET_B_KEY),
        )
        db.commit()
    e2e.sweep()

    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "needs_review", detail


def test_aggregate_comment_retry_after_vcs_failure(org_e2e, monkeypatch):
    """聚合评论发送失败后下一轮 sweep 补发，不永久缺失（复审 Optional）。"""
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-retry", 22520)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    from src.domains.runtime.vcs.api_client import VcsApiClient

    attempts: list[tuple[int, str]] = []

    def _flaky_post(self, issue_number, body_text):
        attempts.append((int(issue_number), body_text or ""))
        if len(attempts) == 1:
            # 真实 VcsApiClient 的网络/上游失败返回 error dict（不抛异常）。
            return {"error": "vcs 502"}
        return {"ok": True}

    monkeypatch.setattr(VcsApiClient, "post_issue_comment", _flaky_post)
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        e2e.complete_child_issue(key, by_repo[key]["child_issue_number"])
    e2e.sweep()

    assert len(attempts) == 1, attempts
    assert e2e.dispatch_detail(dispatch_id)["status"] == "completed"
    # 状态已归一，但评论首轮失败 → 下一轮 sweep 仍会补发
    assert _read_org_issue_status(e2e, 22520) == "pending_review"
    e2e.sweep()
    assert len(attempts) == 2, attempts
    assert all(int(n) == 22520 for n, _ in attempts)
    e2e.sweep()
    assert len(attempts) == 2, attempts


def test_duplicate_sweep_triggers_single_aggregate_per_terminal_status(org_e2e, posted_comments):
    e2e = org_e2e
    body = _bootstrap(e2e, "issue2251-idem", 22517)
    dispatch_id = body["dispatch_id"]
    by_repo = _targets_by_repo(body)

    e2e.complete_child_issue(TARGET_A_KEY, by_repo[TARGET_A_KEY]["child_issue_number"])
    _set_pending_review(TARGET_B_KEY, by_repo[TARGET_B_KEY]["child_issue_number"])
    for _ in range(4):
        e2e.sweep()

    events = _aggregate_events(dispatch_id)
    assert len(events) == 1, events
    keys = {e[2] for e in events}
    assert keys == {f"org_agg:{dispatch_id}:needs_review"}, keys
    assert len([1 for n, _ in posted_comments if int(n) == 22517]) == 1