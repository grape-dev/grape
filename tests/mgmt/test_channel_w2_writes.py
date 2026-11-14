"""W2 渠道写原语（mgmt 归属表）与 runtime→mgmt lib 切换测试。

背景（方案 §3.5）：`grape_t_channel_lease / installation / inbound_dedup /
chat_session_binding / sender_identity` 归 mgmt 域，写方是 runtime IM 进程。
W2 口径 = 调目标域 public 写函数（lib），本地 SQL 仅在 lib 不可用时兜底。

本测试两个方向：
  1. lib 可用：runtime 的 lease 原语必须委托 mgmt lib（不得碰本地连接）；
  2. lib 不可用：仍走原 SQL（沙箱/单库拓扑零回归）。
另测 mgmt 写原语的幂等分支（UPDATE 命中 → 不 INSERT；唯一键占位第二次返回 False）。
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from src.domains.mgmt import public as mgmt_public  # noqa: E402
from src.domains.runtime import public as rt_public  # noqa: E402
from src.kernel import db_registry, domain_read  # noqa: E402


class FakeCursor:
    """记录 SQL 并按脚本返回 rowcount 的假游标。"""

    def __init__(self, rowcounts: list[int], fail_insert: bool = False):
        self.rowcounts = list(rowcounts)
        self.fail_insert = fail_insert
        self.statements: list[tuple[str, tuple]] = []
        self.rowcount = 0
        self.description = []
        self._rows: list[tuple] = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if self.fail_insert and sql.strip().upper().startswith("INSERT"):
            raise RuntimeError("duplicate key")
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def _patch_cursor(monkeypatch, fake: FakeCursor, domains=("mgmt",)):
    @contextmanager
    def _cursor(domain):
        assert domain in domains, domain
        yield fake

    monkeypatch.setattr(db_registry, "cursor", _cursor, raising=True)
    return fake


def test_runtime_lease_delegates_to_mgmt_lib(monkeypatch):
    calls = {}

    class _Stub:
        def acquire_channel_lease(self, lease_key, token, owner, ttl):
            calls["acquire"] = (lease_key, token, owner, ttl)
            return True

        def renew_channel_lease(self, lease_key, token, owner, ttl, expected):
            calls["renew"] = (lease_key, token, expected)
            return True

        def release_channel_lease(self, lease_key, token):
            calls["release"] = (lease_key, token)
            return True

    monkeypatch.setattr(domain_read, "mgmt_write_lib", lambda *a, **k: _Stub(), raising=True)

    def _no_local(*a, **k):
        raise AssertionError("lib 可用时不得走本地 SQL")

    monkeypatch.setattr(db_registry, "cursor", _no_local, raising=True)

    assert rt_public.acquire_channel_lease("feishu:1", "tok", "worker-a", 60) is True
    assert rt_public.renew_channel_lease("feishu:1", "tok", "worker-a", 60, "tok") is True
    assert rt_public.release_channel_lease("feishu:1", "tok") is True
    assert set(calls) == {"acquire", "renew", "release"}
    assert calls["renew"][2] == "tok"


def test_runtime_lease_falls_back_when_lib_missing(monkeypatch):
    monkeypatch.setattr(domain_read, "mgmt_write_lib", lambda *a, **k: None, raising=True)
    fake = _patch_cursor(monkeypatch, FakeCursor([1]), domains=("mgmt", "runtime"))
    assert rt_public.acquire_channel_lease("feishu:2", "tok2") is True
    assert fake.statements and "grape_t_channel_lease" in fake.statements[0][0]


def test_mgmt_acquire_updates_without_insert_when_row_claimed(monkeypatch):
    fake = _patch_cursor(monkeypatch, FakeCursor([1]))
    assert mgmt_public.acquire_channel_lease("feishu:3", "t3") is True
    assert len(fake.statements) == 1, "UPDATE 命中后不应再 INSERT"


def test_mgmt_acquire_inserts_when_no_row(monkeypatch):
    fake = _patch_cursor(monkeypatch, FakeCursor([0, 1]))
    assert mgmt_public.acquire_channel_lease("feishu:4", "t4") is True
    assert len(fake.statements) == 2
    assert fake.statements[1][0].strip().upper().startswith("INSERT")


def test_mgmt_acquire_returns_false_on_insert_race(monkeypatch):
    _patch_cursor(monkeypatch, FakeCursor([0], fail_insert=True))
    assert mgmt_public.acquire_channel_lease("feishu:5", "t5") is False


def test_mgmt_inbound_dedup_claims_once(monkeypatch):
    _patch_cursor(monkeypatch, FakeCursor([1]))
    assert mgmt_public.claim_inbound_dedup("inst-1", "msg-1") is True
    _patch_cursor(monkeypatch, FakeCursor([0]))
    assert mgmt_public.claim_inbound_dedup("inst-1", "msg-1") is False


def test_mgmt_installation_upsert_branches(monkeypatch):
    fake = _patch_cursor(monkeypatch, FakeCursor([1]))
    assert mgmt_public.upsert_channel_installation(
        "inst-2", "feishu", "user", config="{}")["action"] == "updated"

    fake = _patch_cursor(monkeypatch, FakeCursor([0, 0, 1]))
    out = mgmt_public.upsert_channel_installation(
        "inst-3", "feishu", "user", legacy_app_id="cli_x")
    assert out["action"] == "inserted"
    assert any("INSERT" in s.upper() for s, _ in fake.statements)


def test_mgmt_sender_identity_upsert(monkeypatch):
    _patch_cursor(monkeypatch, FakeCursor([1]))
    assert mgmt_public.upsert_sender_identity("i", "u", "grape-1")["action"] == "updated"
    fake = _patch_cursor(monkeypatch, FakeCursor([0]))
    assert mgmt_public.upsert_sender_identity("i", "u2", "grape-2")["action"] == "inserted"
    assert any("INSERT" in s.upper() for s, _ in fake.statements)

def test_runtime_filters_delegate_to_mgmt_lib(monkeypatch):
    calls = {}

    class _Stub:
        def add_event_action_filter(self, fields, created_at):
            calls["add"] = fields
            return 42

        def update_event_action_filter(self, rule_id, fields, updated_at):
            calls["update"] = (rule_id, fields)
            return 1

        def delete_event_action_filter(self, rule_id):
            calls["delete"] = rule_id
            return 1

    monkeypatch.setattr(domain_read, "mgmt_write_lib", lambda *a, **k: _Stub(), raising=True)

    def _no_local(*a, **k):
        raise AssertionError("lib 可用时不得走本地 SQL")

    monkeypatch.setattr(db_registry, "cursor", _no_local, raising=True)
    assert rt_public.add_event_action_filter({"event_type": "e", "action": "a"}, "now") == 42
    assert rt_public.update_event_action_filter(7, {"allow": 1}, "now") == 1
    assert rt_public.delete_event_action_filter(7) == 1
    assert set(calls) == {"add", "update", "delete"}


def test_mgmt_event_filter_rejects_unknown_columns(monkeypatch):
    _patch_cursor(monkeypatch, FakeCursor([1]))
    with pytest.raises(ValueError):
        mgmt_public.update_event_action_filter(1, {"password_hash": "x"}, "now")


def test_mgmt_event_filter_crud_rowcounts(monkeypatch):
    fake = _patch_cursor(monkeypatch, FakeCursor([1, 1, 0]))
    assert mgmt_public.add_event_action_filter(
        {"event_type": "issue", "action": "open", "allow": 1}, "now") >= 0
    assert mgmt_public.update_event_action_filter(3, {"allow": 0}, "now") == 1
    assert mgmt_public.delete_event_action_filter(3) == 0
    assert any("INSERT" in s.upper() for s, _ in fake.statements)
    assert any("DELETE" in s.upper() for s, _ in fake.statements)


def test_runtime_w2_upserts_use_fake_cursor(monkeypatch):
    fake = _patch_cursor(monkeypatch, FakeCursor([1, 0]),
                         domains=("runtime",))
    assert rt_public.upsert_event_active_guard("o/r", "#1", "sre", 5) >= 0
    out = rt_public.upsert_issue_agent_session("sess-1", "o/r", "sre", "#1")
    assert out["action"] in ("inserted", "updated")
    assert any("INSERT" in s.upper() for s, _ in fake.statements)


def test_meta_reads_and_writes_go_through_runtime_lib(monkeypatch):
    """meta 基表在运行域：mgmt 侧 get/set 必须走 owner 域，避免独立拓扑下 1146。"""
    from src.domains.mgmt.user import db as mgmt_db
    from src.kernel import domain_read

    seen = {}

    class _Stub:
        def get_meta(self, key):
            seen["get"] = key
            return "v"

        def set_meta_value(self, key, value):
            seen["set"] = (key, value)

    monkeypatch.setattr(domain_read, "owner_lib", lambda domain, foreign_only=True: _Stub())
    assert mgmt_db.get_meta_value("k1") == "v"
    mgmt_db.set_meta_value("k2", "v2")
    assert seen == {"get": "k1", "set": ("k2", "v2")}
