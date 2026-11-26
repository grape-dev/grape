"""Issue #4445：派发前 /tmp 水位门单元测试。"""
import pytest

from src.domains.runtime.engine import engine_select


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    monkeypatch.setenv("DISPATCH_TMP_HEADROOM_GUARD", "on")
    monkeypatch.setenv("DISPATCH_TMP_MAX_USED_RATIO", "0.9")


def _members():
    return [
        {"runtime_id": 1, "credential_id": 11},
        {"runtime_id": 2, "credential_id": 12},
        {"runtime_id": 3, "credential_id": 0},   # 本机直连
        {"runtime_id": 4, "credential_id": 99},  # 无水位行
    ]


def test_filters_node_at_or_above_90_percent():
    kept = engine_select._filter_by_tmp_headroom(
        _members(), ratios={11: 1.0, 12: 0.5})
    assert [m["runtime_id"] for m in kept] == [2, 3, 4]


def test_local_and_missing_ratio_are_allowed():
    kept = engine_select._filter_by_tmp_headroom(_members(), ratios={11: 0.95})
    assert 3 in [m["runtime_id"] for m in kept]
    assert 4 in [m["runtime_id"] for m in kept]


def test_all_full_returns_empty():
    kept = engine_select._filter_by_tmp_headroom(
        [{"runtime_id": 1, "credential_id": 11}], ratios={11: 0.92})
    assert kept == []


def test_guard_off_disables_filter(monkeypatch):
    monkeypatch.setenv("DISPATCH_TMP_HEADROOM_GUARD", "off")
    members = _members()
    assert engine_select._filter_by_tmp_headroom(members, ratios={11: 0.99}) == members


def test_query_failure_fail_open(monkeypatch):
    monkeypatch.setattr(engine_select, "_tmp_used_ratios_by_credential",
                        lambda ids: None)
    members = _members()
    assert engine_select._filter_by_tmp_headroom(members) == members