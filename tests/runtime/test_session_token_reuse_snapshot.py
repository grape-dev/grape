"""派发/执行前 token 注入：#990 快照复用口径 + #2790 诊断口径回归。

锁四件事：
  1. 快照里有有效 token 时，派发链**只复用**，不调用 issue_session_token（旧实现每次都新签）；
  2. 快照同步返回空（有效 token 拿不到）时记 SESSION_TOKEN_ISSUE_FAILED 并留空，
     绝不新签一枚未回写快照的 token；
  3. 派发上下文缺失（repo_key/agent_type 空）记 SESSION_TOKEN_CONTEXT_MISSING；
  4. 同步钩子自身抛错时由 safe 封装兜底，不阻断派发。
"""
from __future__ import annotations

import pytest

from src.domains.runtime.engine import service as svc


@pytest.fixture()
def snapshot_token(monkeypatch):
    """快照路径可控：session_token_for_execution 返回固定 token，并统计新签次数。"""
    calls = {"issue": 0}

    def _fake_for_execution(task_id, *, session_key_fallback, repo_key, agent_type, user_id=0):
        return "tk_from_snapshot"

    def _fake_issue(**kwargs):
        calls["issue"] += 1
        return "tk_newly_issued"

    monkeypatch.setattr(svc, "session_token_for_execution", _fake_for_execution)
    monkeypatch.setattr(svc, "_persist_task_diagnostic", lambda tid, e: None)
    import src.kernel.auth_sdk.auth_token as auth_token
    monkeypatch.setattr(auth_token, "issue_session_token", _fake_issue)
    return calls


def test_dispatch_reuses_snapshot_token(snapshot_token):
    env = {}
    entry = svc.reuse_or_issue_session_token(
        env, session_key="t_reuse", repo_key="gitee:o/r", agent_type="coding",
        user_id=42, task_id="t_reuse", scope="direct", context_log_level="error")

    assert entry is None
    assert env["GRAPE_SESSION_TOKEN"] == "tk_from_snapshot"
    assert env["GRAPE_CLI_MODE"] == "WORKER"
    assert snapshot_token["issue"] == 0, "派发链不得新签 token，必须复用任务快照"


def test_empty_snapshot_records_issue_failed_without_new_token(monkeypatch):
    records = []
    monkeypatch.setattr(svc, "session_token_for_execution", lambda *a, **k: "")
    monkeypatch.setattr(svc, "_persist_task_diagnostic",
                        lambda tid, entry: records.append((tid, entry)))

    env = {}
    entry = svc.reuse_or_issue_session_token(
        env, session_key="t_empty", repo_key="gitee:o/r", agent_type="coding",
        user_id=1, task_id="t_empty", scope="direct", context_log_level="error")

    assert entry["error_code"] == svc.SESSION_TOKEN_ISSUE_FAILED
    assert "GRAPE_SESSION_TOKEN" not in env, "同步失败不得注入未落库 token"
    assert records and records[0][0] == "t_empty"


def test_missing_context_records_context_missing(monkeypatch):
    records = []
    monkeypatch.setattr(svc, "_persist_task_diagnostic",
                        lambda tid, entry: records.append((tid, entry)))

    env = {}
    entry = svc.reuse_or_issue_session_token(
        env, session_key="t_ctx", repo_key="", agent_type="", task_id="t_ctx",
        scope="dispatch")

    assert entry["error_code"] == svc.SESSION_TOKEN_CONTEXT_MISSING
    assert "GRAPE_SESSION_TOKEN" not in env


def test_snapshot_sync_exception_records_issue_failed(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("lock unavailable")

    records = []
    monkeypatch.setattr(svc, "session_token_for_execution", _boom)
    monkeypatch.setattr(svc, "_persist_task_diagnostic",
                        lambda tid, entry: records.append((tid, entry)))

    env = {}
    entry = svc.reuse_or_issue_session_token(
        env, session_key="t_lock", repo_key="gitee:o/r", agent_type="coding",
        task_id="t_lock", context_log_level="error")

    assert entry["error_code"] == svc.SESSION_TOKEN_ISSUE_FAILED
    assert "lock unavailable" in entry["reason"]
    assert "GRAPE_SESSION_TOKEN" not in env


def test_safe_wrapper_swallows_hook_errors(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr(svc, "reuse_or_issue_session_token", _boom)
    assert svc.reuse_or_issue_session_token_safe({}, log_scope="probe") is None
