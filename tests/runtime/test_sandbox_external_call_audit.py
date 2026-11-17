"""PR #1507 arch-guard F6/C2 regression: cloud sandbox provider external platform
calls must write interface audit events (op + outcome + duration) to the injected
audit chain, and audit write failure must not break the main flow.

Regression: before the fix, CloudProviderBase accepted `audit=` but never used it
(zero write points in cloud_base.py), and CubeSandboxInstance external calls
(_do_connect/_do_kill/_do_is_running/_do_run_command) had no request/result/duration
logs — after a platform-side failure nothing could be reconstructed from logs.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import src.api.main  # noqa: F401 — trigger full import chain
from src.domains.runtime.agents.sandbox.backends.cubesandbox import CubeSandboxProvider


class FakeAudit:
    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


def _make_provider(audit):
    return CubeSandboxProvider(
        audit=audit, credential=None, alert_engine=None,
        config={"api_url": "http://example.invalid", "api_key": "k",
                "sandbox_domain": "cube.app"})


def _fake_sdk_module(state="RUNNING"):
    """伪造 cubesandbox SDK 模块——无需真实 SDK 安装。"""
    mod = MagicMock()
    sdk = MagicMock()
    sdk.get_info.return_value = {"state": state}
    mod.Sandbox.connect.return_value = sdk
    mod.Sandbox.health.return_value = True
    return mod, sdk


class TestProviderExternalCallAudit:

    def test_audit_external_call_writes_event(self):
        audit = FakeAudit()
        p = _make_provider(audit)
        p._audit_external_call("kill_sandbox", "t1", "allow", 12, {"sandbox_id": "sb"})
        assert len(audit.events) == 1
        ev = audit.events[0]
        assert ev.kind.value == "sandbox_access"
        assert ev.outcome == "allow"
        assert ev.detail["op"] == "kill_sandbox"
        assert ev.detail["duration_ms"] == 12
        assert ev.detail["sandbox_id"] == "sb"
        assert ev.task_id == "t1"

    def test_audit_none_is_noop(self):
        p = _make_provider(None)
        p._audit_external_call("kill_sandbox", "t1", "allow", 5)
        # 不抛异常即通过

    def test_connect_sandbox_success_writes_allow_audit(self):
        audit = FakeAudit()
        p = _make_provider(audit)
        with patch("src.domains.runtime.agents.sandbox.backends.cubesandbox."
                   "CubeSandboxInstance._do_connect", lambda self, sid: None):
            p.connect_sandbox("sb-1", issue_id="i-1")
        assert len(audit.events) == 1
        assert audit.events[0].detail["op"] == "connect_sandbox"
        assert audit.events[0].outcome == "allow"
        assert audit.events[0].detail["sandbox_id"] == "sb-1"

    def test_connect_sandbox_failure_writes_deny_audit_and_raises(self):
        audit = FakeAudit()
        p = _make_provider(audit)

        def _boom(self, sid):
            raise RuntimeError("platform down")

        with patch("src.domains.runtime.agents.sandbox.backends.cubesandbox."
                   "CubeSandboxInstance._do_connect", _boom):
            try:
                p.connect_sandbox("sb-2", issue_id="i-2")
                raised = False
            except RuntimeError:
                raised = True
        assert raised, "connect failure must still propagate"
        assert len(audit.events) == 1
        assert audit.events[0].outcome == "deny"
        assert "platform down" in audit.events[0].detail["error"]

    def test_kill_sandbox_writes_audit(self):
        audit = FakeAudit()
        p = _make_provider(audit)
        mod, _sdk = _fake_sdk_module()
        with patch.dict(sys.modules, {"cubesandbox": mod}):
            assert p._do_kill_sandbox("sb-3") is True
        assert len(audit.events) == 1
        assert audit.events[0].detail["op"] == "kill_sandbox"
        assert audit.events[0].outcome == "allow"
        assert audit.events[0].detail["sandbox_id"] == "sb-3"

    def test_kill_sandbox_already_stopped_writes_stopped_audit(self):
        audit = FakeAudit()
        p = _make_provider(audit)
        mod, _sdk = _fake_sdk_module(state="STOPPED")
        with patch.dict(sys.modules, {"cubesandbox": mod}):
            assert p._do_kill_sandbox("sb-4") is True
        assert audit.events[0].outcome == "stopped"

    def test_audit_write_failure_does_not_break_main_flow(self):
        class BoomAudit:
            def log(self, event):
                raise RuntimeError("disk full")

        p = _make_provider(BoomAudit())
        p._audit_external_call("kill_sandbox", "t1", "allow", 5)  # 不抛异常即通过
