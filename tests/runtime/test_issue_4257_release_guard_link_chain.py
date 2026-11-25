"""Issue #4257：PR 评论一致性守卫不得把链路顺序 issue#<seq> 误判为其它 Issue。

复现场景：评论正文描述 A2A 链路顺序
`vod#25105 → issue#25107 → coding#25108`，旧守卫用 `issue#<数字>` 字面匹配，
把 `issue#25107` 当正文主语，与当前任务 Issue 不同即 409 误拦截。

收紧后：仅 `Issue #N` / `Issue N`（显式主语形态）参与断言，且先剔除
`段名#序号` 链路引用；真正引用其它 Issue 的拦截语义不放宽。
"""
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.domains.runtime.vcs import release_guard
import src.api.git_repo_api as git_repo_api


CHAIN = "链路顺序 vod#25105 → issue#25107 → coding#25108"


def _user(**kw):
    base = {"id": 1, "username": "bot", "auth_type": "session_token",
            "agent_type": "coding", "session_key": "t_4257", "repo_key": "gitcode:o/hello"}
    base.update(kw)
    return base


def _wire(monkeypatch, task_issue=25107):
    """记录上游调用，任务 Issue 固定可解析。"""
    calls = []
    monkeypatch.setattr(git_repo_api, "_vcs_req",
                        lambda *a, **k: calls.append((a, k)) or {"id": 999, "body": "ok"})
    monkeypatch.setattr(git_repo_api, "_repo_token", lambda *a, **k: "tok")
    monkeypatch.setattr(release_guard, "resolve_session_task_issue", lambda user: task_issue)
    return calls


# ---------- 单元：锚点解析 ----------

def test_anchor_ignores_link_chain_only_body():
    assert release_guard.first_issue_anchor(CHAIN) is None
    # 更长的连续链同样不产生锚点
    long_chain = "vod#1 → issue#2 → coding#3 → qa#4 → issue#5"
    assert release_guard.first_issue_anchor(long_chain) is None


def test_anchor_accepts_explicit_subject_after_chain():
    body = CHAIN + "\n\n实现 Issue #25107：链路梳理"
    assert release_guard.first_issue_anchor(body) == 25107


def test_anchor_accepts_issue_without_hash():
    assert release_guard.first_issue_anchor("实现 Issue 25107 的修复") == 25107
    assert release_guard.first_issue_anchor("实现 Issue # 25107 的修复") == 25107


def test_anchor_rejects_tight_segment_name_form():
    # `issue#N` / `Issue#N` 属「段名#序号」紧邻形态，不是显式主语。
    assert release_guard.first_issue_anchor("issue#2085 链路引用") is None
    assert release_guard.first_issue_anchor("Issue#2085 链路引用") is None


# ---------- 单元：条件断言三态 ----------

def test_check_allows_link_chain_comment():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=25107, target_issue=25107, body=CHAIN,
        operation="pulls comments create")
    assert ok, msg


def test_check_still_blocks_foreign_explicit_subject():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=25107, target_issue=25107,
        body=CHAIN + "\n\n# Spec: Issue #2085 — 另一个会话的正文",
        operation="pulls comments create")
    assert not ok
    assert "#2085" in msg and "25107" in msg


def test_check_allows_matching_subject_after_chain():
    ok, msg = release_guard.check_issue_consistency(
        task_issue=25107, target_issue=25107,
        body=CHAIN + "\n\n[Coding Agent] 实现 Issue #25107 已完成",
        operation="pulls comments create")
    assert ok, msg


# ---------- 端点：PR 评论写回链路 ----------

def test_pull_comment_with_link_chain_is_written(monkeypatch):
    """验收①：含链路顺序引用的评论正常写入，不 409。"""
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": "[Coding Agent] " + CHAIN + "，链路已接续。"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()


def test_pull_comment_with_foreign_subject_still_blocked(monkeypatch):
    """验收②：主语确为其它 Issue 时仍 409，拦截语义不放宽。"""
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": CHAIN + "\n\n# Spec: Issue #2085 — 另一个会话的正文"})
        assert resp.status_code == 409
        detail = resp.json().get("detail") or {}
        assert detail.get("error_code") == "ISSUE_ASSERT_MISMATCH"
        assert calls == []
    finally:
        app.dependency_overrides.clear()


def test_pull_comment_with_matching_subject_regression(monkeypatch):
    """验收③：主语与当前任务 Issue 一致，既有行为回归不变。"""
    calls = _wire(monkeypatch)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _user()
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/git-repo/o/hello/pulls/1295/comments",
            json={"body": "[Coding Agent] " + CHAIN
                  + "\n\n实现 Issue #25107，关联参考 #2080"})
        assert resp.status_code == 200
        assert calls
    finally:
        app.dependency_overrides.clear()