"""幽灵引擎闸门：未验证执行器的 runtime 不得认领任务（event seq=9491 事故回归）。"""
from src.domains.runtime.engine.service import _has_verified_executor


def test_executor_registered_engine_can_claim():
    assert _has_verified_executor("", "h-7ebafc2ff092") is True
    assert _has_verified_executor("pending", "h-7ebafc2ff092") is True


def test_probed_ok_engine_can_claim():
    assert _has_verified_executor("ok", "") is True


def test_pending_or_failed_without_version_cannot_claim():
    assert _has_verified_executor("pending", "") is False
    assert _has_verified_executor("failed", "") is False


def test_unknown_probe_state_keeps_legacy_behavior():
    # 老库/SQLite 缺列时 probe_status=""，不阻断认领
    assert _has_verified_executor("", "") is True
