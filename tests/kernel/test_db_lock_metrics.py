"""Tests for the kernel-level db-lock instrumentation (TrackedRLock + metrics).

The lock wrapper must stay a drop-in replacement for ``threading.RLock`` while
exposing wait/hold observations so a slow query that wedges every other request
in the same process becomes visible in logs and in ``/metrics``.
"""
from __future__ import annotations

import threading
import time

import pytest

from src.kernel import db_lock_metrics as dlm
from src.kernel.config import path_var, request_id_var


@pytest.fixture(autouse=True)
def _clean_state():
    """Each case starts with an empty holder table and no slow-log noise."""
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    path_var.set("")
    request_id_var.set("-")
    yield
    dlm._STATE.clear()
    dlm._CHILD_CACHE.clear()
    path_var.set("")
    request_id_var.set("-")


def test_reentrant_acquire_and_release_restores_depth():
    lock = dlm.TrackedRLock()
    assert lock.acquire() is True
    assert lock.acquire() is True
    assert len(dlm.snapshot()) == 1
    assert dlm.snapshot()[0]["depth"] == 2
    lock.release()
    assert dlm.snapshot()[0]["depth"] == 1
    lock.release()
    assert dlm.snapshot() == []


def test_context_manager_releases_on_exception():
    lock = dlm.TrackedRLock()
    with pytest.raises(RuntimeError):
        with lock:
            assert dlm.snapshot()
            raise RuntimeError("boom")
    assert dlm.snapshot() == []


def test_second_thread_waits_and_timeout_is_honoured():
    lock = dlm.TrackedRLock()
    acquired = threading.Event()
    done = threading.Event()
    result = {}

    def holder():
        with lock:
            acquired.set()
            done.wait(2)

    def contender():
        acquired.wait(2)
        t0 = time.perf_counter()
        result["ok"] = lock.acquire(timeout=0.2)
        result["waited"] = time.perf_counter() - t0

    holder_t = threading.Thread(target=holder, name="holder")
    contender_t = threading.Thread(target=contender, name="contender")
    holder_t.start()
    contender_t.start()
    contender_t.join(5)
    done.set()
    holder_t.join(5)

    assert result["ok"] is False
    assert result["waited"] >= 0.15


def test_owner_key_prefers_normalized_request_path():
    path_var.set("/api/issues/12345/comments")
    assert dlm._owner_key() == "/api/issues/{id}/comments"

    path_var.set("/api/runtimes/0f3c5b6a-1111-2222-3333-444455556666/heartbeat")
    assert dlm._owner_key().endswith("/{id}/heartbeat")


def test_owner_key_falls_back_to_background_thread_name():
    path_var.set("")
    key = dlm._owner_key()
    assert key.startswith("bg:")


def test_install_registers_collectors_and_records_samples():
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    dlm.install(registry)
    dlm.install(registry)  # idempotent

    path_var.set("/api/scenes/42/with-pipeline")
    lock = dlm.TrackedRLock()
    with lock:
        pass

    assert registry.get_sample_value("grape_db_lock_holds_total", {"owner": "/api/scenes/{id}/with-pipeline"}) == 1.0
    assert registry.get_sample_value("grape_db_lock_holder", {"owner": "/api/scenes/{id}/with-pipeline"}) == 0.0
    assert registry.get_sample_value("grape_db_lock_holds_total_count", {"owner": "/api/scenes/{id}/with-pipeline"}) is None
    total = registry.get_sample_value("grape_db_lock_hold_seconds_count")
    assert total == 1.0
    wait_total = registry.get_sample_value("grape_db_lock_wait_seconds_count")
    assert wait_total == 1.0


def test_slow_hold_logs_warning(monkeypatch):
    messages = []
    monkeypatch.setattr(dlm, "_SLOW_HOLD_S", 0.01)
    monkeypatch.setattr(dlm, "_log", lambda: type("L", (), {"warning": lambda self, msg, *a, **k: messages.append(msg % a)})())

    lock = dlm.TrackedRLock()
    with lock:
        time.sleep(0.03)

    assert any("db_lock slow hold" in m for m in messages)


def test_session_and_owner_locks_use_the_tracked_implementation():
    from src.kernel.owner_db import owner_lock
    from src.kernel.session_manager import db_lock

    assert isinstance(db_lock, dlm.TrackedRLock)
    assert isinstance(owner_lock("runtime"), dlm.TrackedRLock)