"""Issue #1691：A2A handoff 成功响应可读 seq 与下游派发状态。

公开行为 seam：成功派发（派发结果 seq>0）响应带 `event`（seq/status/agent/issue_id），
与幂等/排队响应同口径；`status=failed` 或派发结果 `seq=0`（事件已置 failed）不补 event。
"""
import asyncio
import os

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from src.api.routes import webhook
from src.api.routes.webhook import (
    A2AHandoffBody,
    _a2a_event_payload,
    _augment_a2a_dispatch_response,
    a2a_endpoint,
)


def test_a2a_event_payload_unifies_fields():
    assert _a2a_event_payload(42, "running", "qa", "#1691") == {
        "seq": 42, "status": "running", "agent": "qa", "issue_id": "#1691"}
    assert _a2a_event_payload(7, "queued", "qa", "#1", "2026-11-09") == {
        "seq": 7, "status": "queued", "agent": "qa", "issue_id": "#1",
        "created_at": "2026-11-09"}


def test_augment_adds_event_on_success():
    out = _augment_a2a_dispatch_response(
        {"status": "ok", "seq": 42, "agent": "qa"}, "qa", "1691")
    assert out["event"] == {"seq": 42, "status": "running", "agent": "qa",
                            "issue_id": "#1691"}


def test_augment_keeps_existing_event():
    existing = {"seq": 7, "status": "queued", "agent": "qa", "issue_id": "#1"}
    out = _augment_a2a_dispatch_response(
        {"status": "ok", "seq": 7, "agent": "qa", "event": existing}, "qa", "1")
    assert out["event"] is existing


def test_augment_skips_failed_and_zero_seq():
    assert "event" not in _augment_a2a_dispatch_response(
        {"status": "failed", "seq": 9, "agent": "qa"}, "qa", "1")
    # run_a2a_dispatch 在 auto-select 无引擎/异常时先 _fail_event(seq) 再返回 ok/seq=0
    assert "event" not in _augment_a2a_dispatch_response(
        {"status": "ok", "seq": 0, "agent": "qa"}, "qa", "1")


def _patch_success_path(monkeypatch, dispatch_result):
    monkeypatch.setattr(webhook, "_resolve_a2a_repo_key", lambda repository: "grape-dev/grape")
    monkeypatch.setattr(webhook, "_authorize_a2a",
                        lambda request, repo_key: {"user_id": 1, "username": "u"})
    monkeypatch.setattr(webhook, "_a2a_repo_fallback_check", lambda repo_key, hint: "ok")
    monkeypatch.setattr(webhook, "_a2a_register_repo", lambda repo_key: None)
    monkeypatch.setattr(webhook, "_issue_exists_in_db", lambda repo_key, issue_number: True)
    monkeypatch.setattr(webhook, "_scene_node_check",
                        lambda repo_key, issue_number, agent_name: {"in_scene": True})
    monkeypatch.setattr(webhook, "run_a2a_dispatch",
                        lambda *a, **kw: dict(dispatch_result))
    import src.api.session_store as session_store
    import src.api.event_store as event_store
    monkeypatch.setattr(session_store, "is_agent_enabled", lambda repo_key, agent: True)
    monkeypatch.setattr(event_store, "append_event", lambda **kw: 42)


def test_endpoint_success_response_carries_event_status(monkeypatch):
    _patch_success_path(monkeypatch, {"status": "ok", "seq": 42, "agent": "qa"})
    body = A2AHandoffBody(agent="qa", payload={
        "repository": "grape-dev/grape", "issue_number": "1691", "context": "review"})
    out = asyncio.run(a2a_endpoint(None, body, None))
    assert out["seq"] == 42
    assert out["event"] == {"seq": 42, "status": "running", "agent": "qa",
                            "issue_id": "#1691"}


def test_endpoint_failed_dispatch_has_no_event(monkeypatch):
    _patch_success_path(monkeypatch, {"status": "failed", "seq": 42, "agent": "qa",
                                      "error": "no available engine"})
    body = A2AHandoffBody(agent="qa", payload={
        "repository": "grape-dev/grape", "issue_number": "1691", "context": "review"})
    out = asyncio.run(a2a_endpoint(None, body, None))
    assert out["status"] == "failed"
    assert "event" not in out


def test_endpoint_ok_zero_seq_has_no_event(monkeypatch):
    """auto-select 无引擎/异常：事件 42 已被 _fail_event 置 failed，派发返回 ok/seq=0，
    响应不得再补 event.status=running。"""
    _patch_success_path(monkeypatch, {"status": "ok", "seq": 0, "agent": "qa"})
    body = A2AHandoffBody(agent="qa", payload={
        "repository": "grape-dev/grape", "issue_number": "1691", "context": "review"})
    out = asyncio.run(a2a_endpoint(None, body, None))
    assert out["seq"] == 0
    assert "event" not in out