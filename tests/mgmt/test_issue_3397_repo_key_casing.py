"""Issue #3397：仓库路径大小写口径回归。

覆盖：
- 小写 webhook payload：repository.full_name（VCS 实际路径）优先于
  project.path_with_namespace（可能全小写）；
- repo_key canonical / 大小写不敏感反查（含歧义不合并）；
- events 会话 token 仓库作用域大小写不敏感（403 修复）；
- check_repo_access 大小写不敏感；
- git-repo 路径按仓登记回填实际大小写（全小写路径不再 404）与 404 诊断文案。
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.domains.runtime.vcs.gitcode import GitCodeBackend
from src.kernel.session_manager import db, db_lock

OWNER_ACTUAL = "SongSpace"
REPO_ACTUAL = "Flask-Blog"
FULL_ACTUAL = f"{OWNER_ACTUAL}/{REPO_ACTUAL}"
FULL_LOWER = f"{OWNER_ACTUAL.lower()}/{REPO_ACTUAL.lower()}"
RK_ACTUAL = f"gitcode:{FULL_ACTUAL}"
RK_LOWER = f"gitcode:{FULL_LOWER}"


@pytest.fixture
def managed_repo():
    """登记一个大小写混合仓库；用例结束清理。"""
    from src.domains.mgmt.user import db as user_db
    user_db.add_managed_repo(RK_ACTUAL, OWNER_ACTUAL, REPO_ACTUAL)
    try:
        yield RK_ACTUAL
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (RK_ACTUAL,))
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (RK_LOWER,))
            db.commit()


def _issue_payload(project_path: str, repository_full_name: str) -> bytes:
    return json.dumps({
        "object_attributes": {"action": "open", "title": "t"},
        "project": {"path_with_namespace": project_path, "name": REPO_ACTUAL},
        "repository": {"full_name": repository_full_name, "name": REPO_ACTUAL},
        "user": {"username": "actor"},
    }).encode()


# ---------------------------------------------------------------- webhook 归一

def test_webhook_lowercase_project_uses_repository_full_name():
    """小写 project.path_with_namespace 不得覆盖 repository.full_name 的实际大小写。"""
    evt = GitCodeBackend().normalize(
        _issue_payload(FULL_LOWER, FULL_ACTUAL), "Issue Hook")
    assert evt["repository"] == FULL_ACTUAL


def test_webhook_falls_back_to_project_path_when_repository_missing():
    payload = json.loads(_issue_payload(FULL_ACTUAL, FULL_ACTUAL))
    payload.pop("repository")
    evt = GitCodeBackend().normalize(json.dumps(payload).encode(), "Issue Hook")
    assert evt["repository"] == FULL_ACTUAL


# ---------------------------------------------------------------- repo_key 归一

def test_canonical_repo_key_lowercases_provider_only():
    from src.kernel.repo_key import canonical_repo_key
    assert canonical_repo_key("GitCode:SongSpace/Flask-Blog") == RK_ACTUAL
    assert canonical_repo_key(RK_ACTUAL) == RK_ACTUAL
    assert canonical_repo_key("chat:abc") == "chat:abc"
    assert canonical_repo_key("") == ""


def test_resolve_repo_key_ci_returns_registered_casing(managed_repo):
    from src.kernel.repo_key import canonicalize_repo_key, resolve_repo_key_ci, same_repo_key
    assert resolve_repo_key_ci(RK_LOWER) == RK_ACTUAL
    assert canonicalize_repo_key(RK_LOWER) == RK_ACTUAL
    assert same_repo_key(RK_LOWER, RK_ACTUAL)
    assert not same_repo_key(RK_LOWER, "gitcode:other/repo")


def test_repo_key_candidates_ci_includes_registered_casing(managed_repo):
    """读取侧候选集必须补入登记实际大小写，否则全小写路径漏查存量行。"""
    from src.kernel.repo_key import repo_key_candidates_ci
    cands = repo_key_candidates_ci(FULL_LOWER)
    assert RK_ACTUAL in cands
    assert RK_LOWER in cands


def test_resolve_repo_key_ci_ambiguous_returns_empty(managed_repo):
    """同一 owner 下仅大小写不同的两个仓库不得被合并。"""
    from src.domains.mgmt.user import db as user_db
    from src.kernel.repo_key import resolve_repo_key_ci

    other = f"gitcode:{OWNER_ACTUAL}/{REPO_ACTUAL.upper()}"
    user_db.add_managed_repo(other, OWNER_ACTUAL, REPO_ACTUAL.upper())
    try:
        assert resolve_repo_key_ci(f"gitcode:{OWNER_ACTUAL.lower()}/flask-blog") == ""
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (other,))
            db.commit()


# ---------------------------------------------------------------- events 鉴权

def test_events_session_scope_case_insensitive():
    from fastapi import HTTPException
    from src.domains.runtime.routes.issues import _enforce_session_repo_scope

    user = {"auth_type": "session_token", "repo_key": RK_LOWER}
    # 实际大小写路径（历史 403 场景）→ 放行
    _enforce_session_repo_scope(user, [RK_ACTUAL])
    # 真正跨仓库仍拒绝
    with pytest.raises(HTTPException) as exc:
        _enforce_session_repo_scope(user, ["gitcode:other/repo"])
    assert exc.value.status_code == 403


def test_verify_session_token_for_repo_case_insensitive(monkeypatch):
    from src.kernel.auth_sdk import auth_token
    monkeypatch.setattr(auth_token, "verify_session_token",
                        lambda token: {"repo_key": RK_ACTUAL, "agent_type": "vod"})
    assert auth_token.verify_session_token_for_repo("tk_x", RK_LOWER) is not None
    assert auth_token.verify_session_token_for_repo("tk_x", "gitcode:other/repo") is None


def test_check_repo_access_case_insensitive(managed_repo):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    from src.domains.mgmt.user import db as user_db
    from src.api.deps import check_repo_access

    acc = create_grape_account(username=f"i3397_{uuid.uuid4().hex[:8]}", email="", password="pw")
    user_db.mark_user_repo_join(acc["id"], RK_ACTUAL, is_join=True, is_admin=True)
    try:
        check_repo_access({"id": acc["id"], "username": acc["username"], "auth_type": "jwt"},
                          RK_LOWER)
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_repos WHERE user_id=?", (acc["id"],))
            db.commit()


# ---------------------------------------------------------------- git-repo

@pytest.fixture
def git_repo_client(managed_repo, monkeypatch):
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key

    calls = []

    def _fake_req(method, url, token="", body=None):
        calls.append({"method": method, "url": url, "body": body})
        return {"ok": True, "echo": {"url": url}, "title": "t"}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token",
                        lambda *a, **k: "bot_tok")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 98701, "username": "i3397", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": RK_ACTUAL,
    }
    try:
        yield TestClient(app), calls
    finally:
        app.dependency_overrides.clear()


def test_git_repo_lowercase_path_resolved_to_actual(git_repo_client):
    """全小写路径调用 git-repo issues get → 使用仓登记的实际大小写，不再 404。"""
    client, calls = git_repo_client
    r = client.get(f"/api/git-repo/{OWNER_ACTUAL.lower()}/{REPO_ACTUAL.lower()}/issues/1")
    assert r.status_code == 200, r.text
    assert calls, "上游未被调用"
    assert f"/repos/{OWNER_ACTUAL}/{REPO_ACTUAL}/issues/1" in calls[-1]["url"]


def test_repo_case_mismatch_hint_is_diagnostic(managed_repo):
    """404 兜底诊断文案：命中仓登记但写法不同 → 返回实际路径。"""
    from src.api import git_repo_api

    token = git_repo_api._REPO_PATH_CTX.set(
        ("gitcode", OWNER_ACTUAL.lower(), REPO_ACTUAL.lower()))
    try:
        hint = git_repo_api._repo_case_mismatch_hint()
        assert FULL_ACTUAL in hint
        assert "大小写" in hint
    finally:
        git_repo_api._REPO_PATH_CTX.reset(token)


def test_repo_case_mismatch_hint_reports_ambiguity(managed_repo):
    """歧义（仅大小写不同的多仓）不得静默裸 404，需给出歧义诊断。"""
    from src.api import git_repo_api
    from src.domains.mgmt.user import db as user_db

    other = f"gitcode:{OWNER_ACTUAL}/{REPO_ACTUAL.upper()}"
    user_db.add_managed_repo(other, OWNER_ACTUAL, REPO_ACTUAL.upper())
    token = git_repo_api._REPO_PATH_CTX.set(
        ("gitcode", OWNER_ACTUAL.lower(), REPO_ACTUAL.lower()))
    try:
        hint = git_repo_api._repo_case_mismatch_hint()
        assert "歧义" in hint
        assert FULL_ACTUAL in hint
    finally:
        git_repo_api._REPO_PATH_CTX.reset(token)
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (other,))
            db.commit()


def test_repo_case_mismatch_hint_empty_when_same_casing(managed_repo):
    from src.api import git_repo_api

    token = git_repo_api._REPO_PATH_CTX.set(("gitcode", OWNER_ACTUAL, REPO_ACTUAL))
    try:
        assert git_repo_api._repo_case_mismatch_hint() == ""
    finally:
        git_repo_api._REPO_PATH_CTX.reset(token)


# ---------------------------------------------------------------- REPO_* 注入恒等式
# Issue #3397（QA 复审补充）：注入 env 的 REPO_FULL_NAME 必须与
# REPO_OWNER/REPO_NAME 逐字符一致（且为仓登记的实际大小写）。

def _assert_repo_env_identity(env):
    assert env["REPO_FULL_NAME"] == f"{env['REPO_OWNER']}/{env['REPO_NAME']}"
    assert env["REPO_FULL_NAME"] == FULL_ACTUAL


def test_canonical_repo_identity_helper_returns_actual_casing(managed_repo):
    from src.domains.runtime.pipeline.event_pipeline import _canonical_repo_identity
    rk, full = _canonical_repo_identity(RK_LOWER, FULL_LOWER, where=" (test)")
    assert rk == RK_ACTUAL
    assert full == FULL_ACTUAL


def test_build_task_env_repo_identity(managed_repo):
    from src.domains.runtime.pipeline.event_pipeline import build_task_env
    env = build_task_env(RK_LOWER, "vod", "1")
    _assert_repo_env_identity(env)


def test_create_failed_task_env_repo_identity(managed_repo):
    from src.domains.runtime.pipeline import event_pipeline

    evt = {"type": "issue", "issue_id": "1", "repository": FULL_LOWER,
           "payload": {"issue_number": 1}, "account": "actor"}
    # 函数不返回 task_id（基线语义），按 event_seq 反查快照
    event_pipeline._create_failed_task(
        RK_LOWER, 990001, "vod", "", "vod", "", 0, evt, issue_id="1")
    try:
        with event_pipeline.db_lock:
            row = event_pipeline._rt_db.cursor().execute(
                "SELECT env_extra FROM agent_tasks WHERE event_seq=990001").fetchone()
        assert row, "failed task 快照未落库"
        env = json.loads(row[0])
        _assert_repo_env_identity(env)
    finally:
        with event_pipeline.db_lock:
            event_pipeline._rt_db.cursor().execute(
                "DELETE FROM agent_tasks WHERE event_seq=990001")
            event_pipeline._rt_db.commit()


def test_enqueue_pipeline_task_env_repo_identity(managed_repo):
    """入队 env 恒等式：直接调用 `_enqueue_pipeline_task` 的 env 组装段。

    `_enqueue_pipeline_task` 全链路（引擎选择/DB 入队）不便于单测，故其 env 组装
    已抽为 `_repo_identity_env`（入队函数真实调用的同一段），此处断言该段的
    三元组恒等式，而不是手工拼 env 的空转断言。
    """
    from src.domains.runtime.pipeline import event_pipeline

    rk, env = event_pipeline._repo_identity_env(RK_LOWER, FULL_LOWER)
    assert rk == RK_ACTUAL
    _assert_repo_env_identity(env)


# ---------------------------------------------------------------- events 读路由

def test_list_events_lowercase_path_and_legacy_lower_token(managed_repo):
    """真实 `list_events` 路由：全小写路径 + 历史小写 tk_ token 也能读到实际大小写行。

    回归 QA 复审阻塞项：此前仅测 `issues._enforce_session_repo_scope`，events 路由
    仍精确比对候选 → 全小写路径 403 / 查不到实际大小写 `events.repo_key` 行。
    """
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key

    seq = 990397
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, "
            "actor, payload, agent_type, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (seq, RK_ACTUAL, "issue_opened", "webhook", "1", "t", "s",
             "actor", "{}", "vod", "completed"))
        db.commit()

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "auth_type": "session_token", "repo_key": RK_LOWER,
        "agent_type": "vod", "session_key": "s1", "id": 0,
    }
    try:
        client = TestClient(app)
        r = client.get(f"/api/events/{FULL_LOWER}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert seq in [e["seq"] for e in body["events"]], body
    finally:
        app.dependency_overrides.clear()
        with db_lock:
            db.cursor().execute("DELETE FROM events WHERE seq=?", (seq,))
            db.commit()


def test_list_events_still_rejects_cross_repo_lowercase_token(managed_repo):
    """全小写路径放行不等于跨仓库放行：其它仓库仍 403。"""
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "auth_type": "session_token", "repo_key": RK_LOWER,
        "agent_type": "vod", "session_key": "s1", "id": 0,
    }
    try:
        client = TestClient(app)
        r = client.get("/api/events/other-owner/other-repo")
        assert r.status_code == 403, r.text
    finally:
        app.dependency_overrides.clear()

