"""跨域写基础设施单测：幂等、失败补偿、重试、死信。"""
import pytest

from src.kernel import cross_write as cw


@pytest.fixture(autouse=True)
def _clean():
    cw.ensure_log_table()
    from src.kernel.session_manager import db
    db.cursor().execute(f"DELETE FROM {cw.LOG_TABLE}")
    yield


def _apply_counter():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1

    return calls, fn


def test_submit_applied_once():
    calls, fn = _apply_counter()
    out = cw.submit("runtime", "test_action", "k1", {"a": 1}, fn)
    assert out["status"] == "applied"
    assert calls["n"] == 1
    assert cw.stats().get("applied") == 1


def test_submit_duplicate_key_does_not_reapply():
    calls, fn = _apply_counter()
    cw.submit("runtime", "test_action", "k2", {}, fn)
    out2 = cw.submit("runtime", "test_action", "k2", {}, fn)
    assert out2["status"] == "duplicate"
    assert calls["n"] == 1          # 没有重复执行


def test_submit_failure_recorded():
    def boom():
        raise RuntimeError("target down")

    out = cw.submit("mgmt", "bad_action", "k3", {}, boom)
    assert out["status"] == "failed"
    assert "target down" in out["error"]
    assert cw.stats().get("failed") == 1


def test_drain_retries_and_applies():
    def boom():
        raise RuntimeError("first attempt fails")

    cw.submit("runtime", "retry_action", "k4", {"x": 2}, boom)
    # 让 next_retry_at 到点
    from src.kernel.session_manager import db
    db.cursor().execute(f"UPDATE {cw.LOG_TABLE} SET next_retry_at=0 WHERE idempotency_key='k4'")

    applied = {"n": 0}

    def handler(payload):
        assert payload == {"x": 2}
        applied["n"] += 1

    out = cw.drain({"retry_action": handler})
    assert out["applied"] == 1
    assert applied["n"] == 1
    assert cw.stats().get("applied") == 1


def test_drain_without_handler_keeps_failed():
    def boom():
        raise RuntimeError("x")

    cw.submit("runtime", "no_handler", "k5", {}, boom)
    from src.kernel.session_manager import db
    db.cursor().execute(f"UPDATE {cw.LOG_TABLE} SET next_retry_at=0 WHERE idempotency_key='k5'")
    out = cw.drain({})
    assert out["failed"] == 1
    assert cw.stats().get("failed") == 1


def test_dead_letter_after_max_attempts():
    def boom():
        raise RuntimeError("always")

    cw.submit("runtime", "always_fail", "k6", {}, boom)
    from src.kernel.session_manager import db
    for _ in range(cw.MAX_ATTEMPTS):
        db.cursor().execute(f"UPDATE {cw.LOG_TABLE} SET next_retry_at=0 WHERE idempotency_key='k6'")
        cw.drain({"always_fail": lambda p: (_ for _ in ()).throw(RuntimeError("always"))})
    assert cw.stats().get("dead") == 1


def test_requires_idempotency_key():
    with pytest.raises(ValueError):
        cw.submit("runtime", "x", "", {}, lambda: None)


def test_unique_key_keeps_prefix_and_differs_per_call():
    k1 = cw.unique_key("scene-nodes:1:abc")
    k2 = cw.unique_key("scene-nodes:1:abc")
    assert k1.startswith("scene-nodes:1:abc:")
    assert k1 != k2


def test_content_key_dedupes_forever_unique_key_does_not():
    """状态往复（置 A → 清空 → 再置 A）必须能重放：内容键会被永久去重，唯一键不会。"""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1

    # 内容键：第二次同键直接 duplicate，不再执行
    cw.submit("runtime", "state_set", "state:u1:A", {}, fn)
    assert cw.submit("runtime", "state_set", "state:u1:A", {}, fn)["status"] == "duplicate"
    assert calls["n"] == 1

    # 唯一键：每次都提交（幂等由目标原语负责）
    cw.submit("runtime", "state_set", cw.unique_key("state:u1:A"), {}, fn)
    cw.submit("runtime", "state_set", cw.unique_key("state:u1:A"), {}, fn)
    assert calls["n"] == 3